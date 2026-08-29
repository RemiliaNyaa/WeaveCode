from __future__ import annotations

import asyncio
from typing import Any

from weavecode.core.permissions.manager import PermissionManager
from weavecode.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    evaluate_hard,
    evaluate_soft,
)

# bash / write_file 默认询问；测审批/缓存/超时机制时，
# 用这个 helper 把 bash 固定为 ASK，避免依赖默认值。
def _ask_bash_policies() -> dict[str, ToolPolicy]:
    merged = dict(DEFAULT_POLICIES)
    merged["bash"] = ToolPolicy(default=PermissionDecision.ASK)
    return merged


def _make_manager(**policies: ToolPolicy) -> PermissionManager:
    # policy_file=None：不读写用户真实规则文件，测试不污染 ~/.weave
    merged = _ask_bash_policies()
    merged.update(policies)
    return PermissionManager(merged, policy_file=None)


async def _collect_emitted() -> tuple[list[dict[str, Any]], Any]:
    emitted: list[dict[str, Any]] = []

    async def emitter(event: dict[str, Any]) -> None:
        emitted.append(event)

    return emitted, emitter


# ── evaluate 委托 ─────────────────────────────────────────────────────────────

# 功能：验证默认策略（DEFAULT_POLICIES）与 evaluate 委托
# 设计：只读工具默认放行、影响外部世界的操作默认询问；未登记工具走 ASK 兜底
def test_evaluate_delegates_to_policy() -> None:
    mgr = PermissionManager(policy_file=None)
    assert mgr.evaluate("read_file", {"path": "x"}) == PermissionDecision.ALLOW
    assert mgr.evaluate("bash", {"command": "echo hi"}) == PermissionDecision.ASK
    assert mgr.evaluate("write_file", {"path": "x", "content": ""}) == PermissionDecision.ASK
    # 未登记工具（如 MCP）→ ASK 兜底
    assert mgr.evaluate("exa__web_search", {"query": "x"}) == PermissionDecision.ASK



# 功能：验证 evaluate_hard 处理危险命令黑名单（越界此时仍在硬规则内）
# 设计：危险命令返回 ASK；普通命令返回 None；非 bash 无硬规则
def test_evaluate_hard_dangerous_only() -> None:
    bash = ToolPolicy(default=PermissionDecision.ALLOW)
    assert evaluate_hard("bash", {"command": "ls"}, bash) is None
    assert evaluate_hard("bash", {"command": "rm -rf /"}, bash) == PermissionDecision.ASK
    assert evaluate_hard("bash", {"command": "cat /etc/hosts"}, bash) == PermissionDecision.ASK
    assert evaluate_hard("read_file", {"path": "/etc/hosts"}, bash) is None  # 非 bash 无硬规则


# 功能：验证 evaluate_soft 直接返回该工具的默认策略
# 设计：软规则不读任何名单，只返回传入策略的 default
def test_evaluate_soft_returns_default() -> None:
    assert evaluate_soft("bash", {"command": "ls -la"}, ToolPolicy(default=PermissionDecision.ALLOW)) == PermissionDecision.ALLOW
    assert evaluate_soft("bash", {"command": "cat x"}, ToolPolicy(default=PermissionDecision.ASK)) == PermissionDecision.ASK
    assert evaluate_soft("read_file", {"path": "x"}, ToolPolicy(default=PermissionDecision.ALLOW)) == PermissionDecision.ALLOW

# ── check_and_wait: ALLOW path ───────────────────────────────────────────────

# 功能：验证策略为 ALLOW 时 check_and_wait 立即放行，不发任何事件
# 设计：read_file 默认 ALLOW，断言不产生 permission.requested 事件，覆盖"无噪声放行"路径
async def test_check_and_wait_allow_no_event() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t1", tool_name="read_file",
        params={"path": "README.md"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is True
    assert decision == "auto_allow"
    assert emitted == []


# ── check_and_wait: ASK path + respond ───────────────────────────────────────

# 功能：验证 ASK 策略时发出 permission.requested 事件并等待 respond() 解决 Future
# 设计：在后台协程中调用 respond("allow_once")，主协程 await 结束后断言结果；
#       这是权限系统的核心反向请求通路
async def test_check_and_wait_ask_emits_event_and_waits() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_respond() -> None:
        await asyncio.sleep(0)  # yield once so check_and_wait can emit the event
        mgr.respond("t2", "allow_once")

    task = asyncio.create_task(_auto_respond())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t2", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is True
    assert decision == "allow_once"
    assert len(emitted) == 1
    assert emitted[0]["type"] == "permission.requested"
    assert emitted[0]["tool_use_id"] == "t2"
    assert emitted[0]["tool_name"] == "bash"


# 功能：验证 respond("reject_once") 使 check_and_wait 返回 allowed=False
# 设计：用户拒绝时工具不应执行，确认 False 返回值而不是异常（拒绝不终止循环由上层处理）
async def test_check_and_wait_reject_once_returns_false() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    async def _auto_deny() -> None:
        await asyncio.sleep(0)
        mgr.respond("t3", "reject_once")

    task = asyncio.create_task(_auto_deny())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t3", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is False
    assert decision == "reject_once"


# ── always_allow cache ───────────────────────────────────────────────────────

# 功能：验证 respond("always_allow") 后同 session 同权限下次不再发事件
# 设计：第二次调用 check_and_wait 命中 always 缓存，直接 auto_allow，emitted 仍为 1 条
async def test_always_allow_skips_future_ask() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t4", "always_allow")

    task = asyncio.create_task(_auto_always())
    allowed1, _ = await mgr.check_and_wait(
        tool_use_id="t4", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task
    assert allowed1 is True

    allowed2, decision2 = await mgr.check_and_wait(
        tool_use_id="t5", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed2 is True
    assert decision2 == "auto_allow"
    assert len(emitted) == 1  # only the first call emitted an event


# 功能：验证 always_allow 在同一 manager 实例内跨 session 生效（持久化规则共享）
# 设计：s1 设置 always_allow → 写入持久化规则；s2 命中已保存规则，直接放行；emitted 只有 1 条
async def test_always_allow_shared_across_sessions() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t6", "always_allow")

    task = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="t6", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t7", tool_name="bash",
        params={"command": "echo"}, session_id="s2",
        event_emitter=emitter,
    )

    assert allowed is True
    assert decision == "auto_allow"
    assert len(emitted) == 1  # s2 命中已保存规则，不再发出事件


# ── cancel_session ────────────────────────────────────────────────────────────

# 功能：验证 cancel_session 将 pending Future 设为 reject_once，check_and_wait 返回 False
# 设计：模拟客户端断连场景——check_and_wait 挂起后调用 cancel_session，
#       确认 Future 被解决而非永久挂起（防止僵尸 run）
async def test_cancel_session_resolves_pending_future() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    async def _cancel_after_emit() -> None:
        await asyncio.sleep(0)  # wait for event to be emitted
        mgr.cancel_session("s1", reason="client_disconnected")

    task = asyncio.create_task(_cancel_after_emit())
    allowed, _ = await mgr.check_and_wait(
        tool_use_id="t10", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is False


# 功能：验证 cancel_session 只取消属于该 session 的 pending Future
# 设计：s1 和 s2 各有一个 pending，cancel_session(s2) 不影响 s1 的 Future
async def test_cancel_session_only_affects_target_session() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    s1_done = asyncio.Event()
    s2_done = asyncio.Event()
    s1_result: list[bool] = []
    s2_result: list[bool] = []

    async def _s1() -> None:
        allowed, _ = await mgr.check_and_wait(
            tool_use_id="ta", tool_name="bash",
            params={"command": "echo"}, session_id="s1",
            event_emitter=emitter,
        )
        s1_result.append(allowed)
        s1_done.set()

    async def _s2() -> None:
        allowed, _ = await mgr.check_and_wait(
            tool_use_id="tb", tool_name="bash",
            params={"command": "echo"}, session_id="s2",
            event_emitter=emitter,
        )
        s2_result.append(allowed)
        s2_done.set()

    t1 = asyncio.create_task(_s1())
    t2 = asyncio.create_task(_s2())

    await asyncio.sleep(0)  # let both emit events and hang

    mgr.cancel_session("s2")
    await s2_done.wait()

    mgr.respond("ta", "allow_once")
    await s1_done.wait()

    await t1
    await t2

    assert s1_result == [True]   # s1 was allowed
    assert s2_result == [False]  # s2 was cancelled → denied


# ── respond: unknown tool_use_id ──────────────────────────────────────────────

# 功能：验证 respond 传入不存在的 tool_use_id 时静默忽略，不抛异常
# 设计：竞态场景（客户端重复发送响应）不应导致 daemon crash
def test_respond_unknown_tool_use_id_is_noop() -> None:
    mgr = _make_manager()
    mgr.respond("nonexistent", "allow_once")  # should not raise


# ── 审批超时 ──────────────────────────────────────────────────────────────────

# 功能：验证 check_and_wait 超时后返回 allowed=False、decision="timeout"，不永久挂起
# 设计：timeout_s=0.05 极短超时，不主动 respond；断言在合理时间内返回
async def test_permission_timeout_returns_false() -> None:
    mgr = PermissionManager(
        policies=_ask_bash_policies(), policy_file=None, timeout_s=0.05
    )
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t_timeout", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is False
    assert decision == "timeout"
    assert len(emitted) == 1
    assert emitted[0]["type"] == "permission.requested"


# 功能：验证超时后 pending 被清理，迟到的 respond 不影响后续调用
# 设计：超时后调用 respond，不抛异常（unknown tool_use_id 静默忽略）；
#       并断言 pending 表已清空
async def test_permission_timeout_cleans_up_pending() -> None:
    mgr = PermissionManager(
        policies=_ask_bash_policies(), policy_file=None, timeout_s=0.05
    )
    _, emitter = await _collect_emitted()

    await mgr.check_and_wait(
        tool_use_id="t_late", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    # 超时后迟到的 respond 不应 crash
    mgr.respond("t_late", "allow_once")  # should be noop
    assert "t_late" not in mgr._pending
