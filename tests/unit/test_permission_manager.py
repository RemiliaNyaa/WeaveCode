from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from weavecode.core.permissions.manager import EXTERNAL_DIRECTORY, PermissionManager
from weavecode.core.permissions.paths import approval_dir, extract_paths, is_within
from weavecode.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    evaluate_hard,
    evaluate_soft,
)
from weavecode.core.permissions.storage import load_policy, save_rule
from weavecode.core.storage import Database, apply_migrations

# ── helpers ──────────────────────────────────────────────────────────────────

# 工具层默认已改为全部 ALLOW（对齐 opencode）；测审批/缓存/超时机制时，
# 用这个 helper 在完整默认策略之上把 bash 设为 ASK，避免依赖默认值。
def _ask_bash_policies() -> dict[str, ToolPolicy]:
    merged = dict(DEFAULT_POLICIES)
    merged["bash"] = ToolPolicy(default=PermissionDecision.ASK)
    return merged


def _make_manager(**policies: ToolPolicy) -> PermissionManager:
    # db=None：测试中不使用持久化，不污染 ~/.weave/weave.db
    merged = _ask_bash_policies()
    merged.update(policies)
    return PermissionManager(merged)


# 造一个建好表、指向临时目录的 db（权限持久化测试用）
async def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    await apply_migrations(db)
    return db


async def _collect_emitted() -> tuple[list[dict[str, Any]], Any]:
    emitted: list[dict[str, Any]] = []

    async def emitter(event: dict[str, Any]) -> None:
        emitted.append(event)

    return emitted, emitter


# ── evaluate 委托与 hard/soft 纯函数 ─────────────────────────────────────────

# 功能：验证默认策略（DEFAULT_POLICIES）与 evaluate 委托
# 设计：工具层默认全部 ALLOW（对齐 opencode）；未登记工具走 ASK 兜底；验证委托路径正确
def test_evaluate_delegates_to_policy() -> None:
    mgr = PermissionManager()
    assert mgr.evaluate("read_file", {"path": "x"}) == PermissionDecision.ALLOW
    assert mgr.evaluate("bash", {"command": "echo hi"}) == PermissionDecision.ALLOW
    assert mgr.evaluate("write_file", {"path": "x", "content": ""}) == PermissionDecision.ALLOW
    # 未登记工具（如 MCP）→ ASK 兜底
    assert mgr.evaluate("exa__web_search", {"query": "x"}) == PermissionDecision.ASK


# 功能：验证 evaluate_hard 只处理危险命令黑名单；越界走 tree-sitter + external_directory
# 设计：危险命令返回 ASK；普通命令 / 绝对路径命令返回 None；非 bash 无硬规则
def test_evaluate_hard_dangerous_only() -> None:
    bash = ToolPolicy(default=PermissionDecision.ALLOW)
    assert evaluate_hard("bash", {"command": "ls"}, bash) is None
    assert evaluate_hard("bash", {"command": "rm -rf /"}, bash) == PermissionDecision.ASK
    assert evaluate_hard("bash", {"command": "cat /etc/hosts"}, bash) is None  # 越界不走 hard
    assert evaluate_hard("read_file", {"path": "/etc/hosts"}, bash) is None  # 非 bash 无硬规则


# 功能：验证 evaluate_soft 直接返回该工具的默认策略
# 设计：软规则不再读任何名单，只返回 default
def test_evaluate_soft_returns_default() -> None:
    assert evaluate_soft("bash", {"command": "ls -la"}, ToolPolicy(default=PermissionDecision.ALLOW)) == PermissionDecision.ALLOW
    assert evaluate_soft("bash", {"command": "cat x"}, ToolPolicy(default=PermissionDecision.ASK)) == PermissionDecision.ASK
    assert evaluate_soft("read_file", {"path": "x"}, ToolPolicy(default=PermissionDecision.ALLOW)) == PermissionDecision.ALLOW


# ── paths 工具函数 ───────────────────────────────────────────────────────────

# 功能：验证 is_within 区分目录内外的路径
# 设计：覆盖「同路径」「子路径」「父路径」「兄弟目录」「跨盘符」四类边界
def test_is_within_paths() -> None:
    root = "/proj"
    assert is_within("/proj/a.py", root) is True
    assert is_within("/proj/sub/a.py", root) is True
    assert is_within("/proj", root) is True
    assert is_within("/other/a.py", root) is False
    assert is_within("/proj2/a.py", root) is False  # 前缀相似但不是子目录


# 功能：验证 extract_paths 从工具参数中提取路径并归一化
# 设计：path 键命中；相对路径按工作目录补全；无路径参数的工具返回空
def test_extract_paths() -> None:
    assert extract_paths("read_file", {"path": "a.py"}, "/proj") == [
        os.path.normpath("/proj/a.py")
    ]
    assert extract_paths("update_plan", {"tasks": []}, "/proj") == []


# 功能：验证 approval_dir 对文件取父目录、对目录取自身
# 设计：越界批准是目录级粒度，文件路径需升格到父目录
def test_approval_dir() -> None:
    assert approval_dir("/data/a.txt", is_dir=False) == os.path.normpath("/data")
    assert approval_dir("/data/sub", is_dir=True) == os.path.normpath("/data/sub")


# ── check_and_wait: ALLOW path ───────────────────────────────────────────────

# 功能：验证策略为 ALLOW 时 check_and_wait 立即返回 auto_allow，不发任何事件
# 设计：read_file 默认 ALLOW，断言不产生 permission.requested 事件，覆盖"无噪声放行"路径
async def test_check_and_wait_allow_no_event() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    res = await mgr.check_and_wait(
        tool_use_id="t1", tool_name="read_file",
        params={"path": "README.md"}, session_id="s1",
        event_emitter=emitter,
    )

    assert res.allowed is True
    assert res.decision == "auto_allow"
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
    res = await mgr.check_and_wait(
        tool_use_id="t2", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert res.allowed is True
    assert res.decision == "allow_once"
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
    res = await mgr.check_and_wait(
        tool_use_id="t3", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert res.allowed is False
    assert res.decision == "reject_once"


# ── always_allow cache ────────────────────────────────────────────────────────

# 功能：验证 respond("always_allow") 后同 session 同权限下次不再发事件
# 设计：第二次调用 check_and_wait 命中 always 缓存，直接返回 auto_allow，emitted 仍为 1 条
async def test_always_allow_skips_future_ask() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t4", "always_allow")

    task = asyncio.create_task(_auto_always())
    r1 = await mgr.check_and_wait(
        tool_use_id="t4", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task
    assert r1.allowed is True

    r2 = await mgr.check_and_wait(
        tool_use_id="t5", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )

    assert r2.allowed is True
    assert r2.decision == "auto_allow"
    assert len(emitted) == 1  # only the first call emitted an event


# 功能：验证 always_allow 在同一 manager 实例内跨 session 生效（持久化规则共享）
# 设计：s1 设置 always_allow → 写入 _saved；s2 命中已保存规则，直接放行；emitted 只有 1 条
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

    r = await mgr.check_and_wait(
        tool_use_id="t7", tool_name="bash",
        params={"command": "echo"}, session_id="s2",
        event_emitter=emitter,
    )

    assert r.allowed is True
    assert r.decision == "auto_allow"
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
    res = await mgr.check_and_wait(
        tool_use_id="t10", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert res.allowed is False


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
        res = await mgr.check_and_wait(
            tool_use_id="ta", tool_name="bash",
            params={"command": "echo"}, session_id="s1",
            event_emitter=emitter,
        )
        s1_result.append(res.allowed)
        s1_done.set()

    async def _s2() -> None:
        res = await mgr.check_and_wait(
            tool_use_id="tb", tool_name="bash",
            params={"command": "echo"}, session_id="s2",
            event_emitter=emitter,
        )
        s2_result.append(res.allowed)
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


# ── OUTSIDE_CWD 不被 always 缓存绕过 ─────────────────────────────────────────

# 功能：验证 bash 的工具级 always_allow 之后，操作工作目录外路径的命令仍触发越界审批
# 设计：先对 bash 记「始终允许」（工具级），再用含绝对路径的命令请求；
#       越界检查在工具级缓存之前，应发出 external_directory 事件，不被缓存绕过
async def test_always_allow_bash_does_not_bypass_outside_dir() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t_always", "always_allow")

    t = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="t_always", tool_name="bash",
        params={"command": "echo ok"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )
    await t
    assert len(emitted) == 1  # 首次 ASK 触发事件

    async def _auto_respond_abs() -> None:
        await asyncio.sleep(0)
        mgr.respond("t_abs", "allow_once")

    t2 = asyncio.create_task(_auto_respond_abs())
    res = await mgr.check_and_wait(
        tool_use_id="t_abs", tool_name="bash",
        params={"command": "cat /etc/hosts"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )
    await t2

    assert res.allowed is True
    assert len(emitted) == 2  # 越界命令再次触发 ASK，共 2 个事件
    assert emitted[1]["permission"] == EXTERNAL_DIRECTORY


# 功能：验证 bash 命令中的越界路径被 tree-sitter 提取并先触发越界审批
# 设计：working_dir=/proj，`cat /etc/hosts` 的绝对路径参数被提取 → 先问 external_directory；
#       随后 bash 工具级也 ASK（默认策略），故对每个请求都自动 allow_once（与 opencode 同为两次审批）
async def test_bash_outside_path_triggers_ask() -> None:
    mgr = _make_manager()
    emitted: list[dict[str, Any]] = []

    async def emitter(event: dict[str, Any]) -> None:
        emitted.append(event)
        mgr.respond(event["tool_use_id"], "allow_once")

    res = await mgr.check_and_wait(
        tool_use_id="b1", tool_name="bash",
        params={"command": "cat /etc/hosts"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )

    assert res.allowed is True
    assert emitted[0]["permission"] == EXTERNAL_DIRECTORY
    assert emitted[0]["resource"] == os.path.normpath("/etc")


# ── 越界横切权限（external_directory） ────────────────────────────────────────

# 功能：验证读取工作目录之外的路径会触发 external_directory 审批
# 设计：working_dir=/proj，read_file 请求 /data/a.txt 越界；断言事件 permission 为
#       external_directory 且 resource 为目录 /data（目录级粒度）
async def test_outside_working_dir_triggers_ask() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_allow() -> None:
        await asyncio.sleep(0)
        mgr.respond("o1", "allow_once")

    task = asyncio.create_task(_auto_allow())
    res = await mgr.check_and_wait(
        tool_use_id="o1", tool_name="read_file",
        params={"path": "/data/a.txt"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )
    await task

    assert res.allowed is True
    assert len(emitted) == 1
    assert emitted[0]["permission"] == EXTERNAL_DIRECTORY
    assert emitted[0]["resource"] == os.path.normpath("/data")


# 功能：验证工作目录内的路径不触发越界审批
# 设计：read_file 请求 /proj/a.txt（在工作目录内），read_file 默认 ALLOW，不应发事件
async def test_inside_working_dir_no_ask() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    res = await mgr.check_and_wait(
        tool_use_id="o2", tool_name="read_file",
        params={"path": "/proj/a.txt"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )

    assert res.allowed is True
    assert emitted == []


# 功能：验证对某越界目录选择 always_allow 后，同目录不再询问
# 设计：第一次批准 /data 后写入 _saved；第二次访问 /data/b.txt 命中目录包含，直接放行
async def test_outside_always_allow_remembers_directory() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("o3", "always_allow")

    task = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="o3", tool_name="read_file",
        params={"path": "/data/a.txt"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )
    await task
    assert len(emitted) == 1

    res = await mgr.check_and_wait(
        tool_use_id="o4", tool_name="read_file",
        params={"path": "/data/b.txt"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )

    assert res.allowed is True
    assert res.decision == "auto_allow"
    assert len(emitted) == 1  # 同目录已批准，不再询问


# 功能：验证越界批准按项目隔离——A 项目的批准不影响 B 项目
# 设计：在 /projA 下批准 /data；切到 /projB 访问 /data 时应重新询问（project 不同不命中）
async def test_outside_approval_is_project_scoped() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("o5", "always_allow")

    task = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="o5", tool_name="read_file",
        params={"path": "/data/a.txt"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/projA",
    )
    await task
    assert len(emitted) == 1

    async def _auto_once() -> None:
        await asyncio.sleep(0)
        mgr.respond("o6", "allow_once")

    task2 = asyncio.create_task(_auto_once())
    res = await mgr.check_and_wait(
        tool_use_id="o6", tool_name="read_file",
        params={"path": "/data/a.txt"}, session_id="s2",
        event_emitter=emitter,
        working_dir="/projB",
    )
    await task2

    assert res.allowed is True
    assert len(emitted) == 2  # 不同项目 → 需重新批准


# 功能：验证子 agent 继承父会话的越界批准（同一 manager + 同一 working_dir）
# 设计：父会话批准 /data 后，子 agent 用同一 session/working_dir 访问 /data 直接放行
async def test_subagent_inherits_outside_approval() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("o7", "always_allow")

    task = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="o7", tool_name="read_file",
        params={"path": "/data/a.txt"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )
    await task

    # 子 agent 用同一 session_id（SpawnAgentTool 传入父 session_id）
    res = await mgr.check_and_wait(
        tool_use_id="o8", tool_name="read_file",
        params={"path": "/data/c.txt"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )

    assert res.allowed is True
    assert res.decision == "auto_allow"


# ── 持久化 always 落库 ────────────────────────────────────────────────────────

# 功能：验证 always_allow 决策写进 permission 表，新 PermissionManager 加载后自动放行
# 设计：用 tmp_path 建真实 db，断言表里记录了 (project, permission, resource)；
#       再新建 manager 并传入 load_policy 读出的规则，同项目同权限无需 ASK 直接返回 auto_allow
async def test_persistent_always_saved_and_reloaded(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    mgr = PermissionManager(policies=_ask_bash_policies(), db=db)
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("tp1", "always_allow")

    t = asyncio.create_task(_auto_always())
    res = await mgr.check_and_wait(
        tool_use_id="tp1", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )
    await t
    assert res.allowed is True
    assert ("/proj", "bash", "*") in await load_policy(db)

    # 新 manager 用同一 db 读出的规则，同项目 bash 应直接 auto_allow（无 OUTSIDE_CWD）
    mgr2 = PermissionManager(
        policies=_ask_bash_policies(), db=db, saved=await load_policy(db)
    )
    emitted2, emitter2 = await _collect_emitted()
    res2 = await mgr2.check_and_wait(
        tool_use_id="tp2", tool_name="bash",
        params={"command": "echo new"}, session_id="s2",
        event_emitter=emitter2,
        working_dir="/proj",
    )
    assert res2.allowed is True
    assert res2.decision == "auto_allow"
    assert emitted2 == []  # 无需 ASK


# 功能：同一三元组重复写入不产生重复行（唯一索引 + DO NOTHING 幂等）
# 设计：直接对同一规则调两次 save_rule，断言 permission 表只有一行 —— 直测 upsert 幂等，不绕道审批流程
async def test_save_rule_is_idempotent(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await save_rule(db, ("/proj", "bash", "*"))
    await save_rule(db, ("/proj", "bash", "*"))
    assert await load_policy(db) == [("/proj", "bash", "*")]


# ── 审批超时 ──────────────────────────────────────────────────────────────────

# 功能：验证 check_and_wait 超时后返回 allowed=False、decision="timeout"，不永久挂起
# 设计：timeout_s=0.05 极短超时，不主动 respond；断言在合理时间内返回
async def test_permission_timeout_returns_false() -> None:
    mgr = PermissionManager(policies=_ask_bash_policies(), timeout_s=0.05)
    emitted, emitter = await _collect_emitted()

    res = await mgr.check_and_wait(
        tool_use_id="t_timeout", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )

    assert res.allowed is False
    assert res.decision == "timeout"
    assert len(emitted) == 1
    assert emitted[0]["type"] == "permission.requested"


# 功能：验证超时后 pending 被清理，迟到的 respond 不影响后续调用
# 设计：超时后调用 respond，不抛异常（unknown tool_use_id 静默忽略）；
#       并断言 pending 表已清空
async def test_permission_timeout_cleans_up_pending() -> None:
    mgr = PermissionManager(policies=_ask_bash_policies(), timeout_s=0.05)
    _, emitter = await _collect_emitted()

    await mgr.check_and_wait(
        tool_use_id="t_late", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    # 超时后迟到的 respond 不应 crash
    mgr.respond("t_late", "allow_once")  # should be noop
    assert "t_late" not in mgr._pending


# ── 危险命令黑名单：硬规则，不可被「始终允许」绕过 ─────────────────────────────

# 功能：验证危险命令（rm -rf）即使已「始终允许 bash」也强制审批
# 设计：先对 bash 记 always_allow；再执行 rm -rf /tmp/x；
#       危险黑名单是硬规则、排在缓存之前，应仍发出 permission.requested
async def test_dangerous_command_asks_even_after_always_allow() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("d1", "always_allow")

    t = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="d1", tool_name="bash",
        params={"command": "echo ok"}, session_id="s1",
        event_emitter=emitter,
    )
    await t
    assert len(emitted) == 1  # 首次 ASK 并记住 always_allow

    async def _auto_deny() -> None:
        await asyncio.sleep(0)
        mgr.respond("d2", "reject_once")

    t2 = asyncio.create_task(_auto_deny())
    res = await mgr.check_and_wait(
        tool_use_id="d2", tool_name="bash",
        params={"command": "rm -rf /tmp/x"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )
    await t2

    assert res.allowed is False
    assert len(emitted) == 2  # 危险命令仍触发审批，未被 always_allow 绕过


# 功能：验证危险命令在工作目录内也拦截（补上纯越界方案拦不住的场景）
# 设计：`rm -rf ./build` 路径在工作目录内（越界层不拦），但命中危险黑名单 → 强制审批
async def test_dangerous_command_inside_working_dir_asks() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_allow() -> None:
        await asyncio.sleep(0)
        mgr.respond("d3", "allow_once")

    t = asyncio.create_task(_auto_allow())
    res = await mgr.check_and_wait(
        tool_use_id="d3", tool_name="bash",
        params={"command": "rm -rf ./build"}, session_id="s1",
        event_emitter=emitter,
        working_dir="/proj",
    )
    await t

    assert res.allowed is True
    assert len(emitted) == 1
    assert emitted[0]["permission"] == "bash"  # 工具层（危险黑名单）触发，而非越界层
