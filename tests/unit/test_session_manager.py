from __future__ import annotations

from pathlib import Path

import pytest

from weavecode.core.bus.envelope import HandlerError
from weavecode.core.events.bus import EventBus
from weavecode.core.runner import RunOutcome
from weavecode.core.session.manager import SESSION_CLOSED, SESSION_NOT_FOUND, SessionManager
from weavecode.core.session.model import Session
from weavecode.core.session.store import SessionStore
from weavecode.core.storage import Database, apply_migrations


class _Runner:
    # 模拟 AgentRunner，将 run 新消息写入 thread 后返回成功
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        plan_storage: object | None = None,
    ) -> RunOutcome:
        assert run_id is not None
        assert session is not None
        assert store is not None
        await store.append_messages(
            session.id,
            [{"role": "assistant", "content": [{"type": "text", "text": f"done {goal}"}]}],
            run_id,
        )
        return RunOutcome(status="success", result="done", reason=None)


# 造一个建好表、指向临时目录的 store（测试用）
async def _store(tmp_path: Path) -> SessionStore:
    db = Database(tmp_path / "test.db")
    await apply_migrations(db)
    return SessionStore(db)

# 功能：验证 create 会创建 active session、写入 meta 并发布 session.created 事件
# 设计：用真实 SessionStore + EventBus 收集事件，覆盖 manager 与 store/bus 的协作边界
async def test_create_session_writes_meta_and_event(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = await _store(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]

    session = await manager.create("chat", "title")

    assert session.status == "active"
    assert (await store.read_meta(session.id)).title == "title"
    assert [e.type for e in events] == ["session.created"]  # type: ignore[attr-defined]


# 功能：验证 chat session 处理一条消息后进入 waiting_for_input，并保留 user/assistant thread
# 设计：mock runner 主动追加 assistant 消息，确认 send_message 负责 user 消息、状态流转和 run_id 记录
async def test_send_message_chat_enters_waiting_and_writes_thread(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")

    run_id = await manager.send_message(session.id, "hello")

    loaded = await store.read_meta(session.id)
    assert loaded.status == "waiting_for_input"
    assert loaded.run_ids == [run_id]
    messages = await store.read_messages(session.id)
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1]["role"] == "assistant"


# 功能：验证 one_shot session 在单次消息完成后自动 closed
# 设计：复用 mock runner 的成功路径，聚焦 mode 对最终状态的影响，保证 weave run 的统一路径正确
async def test_one_shot_auto_closes(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("one_shot")

    await manager.send_message(session.id, "hello")

    assert (await store.read_meta(session.id)).status == "closed"


# 功能：验证不存在的 session_id 返回 session_not_found 错误码
# 设计：直接调用 get_history 的查找路径，断言 HandlerError code，覆盖 IPC handler 可结构化返回错误
async def test_missing_session_raises_handler_error(tmp_path: Path) -> None:
    manager = SessionManager(await _store(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    with pytest.raises(HandlerError) as exc:
        await manager.get_history("missing")
    assert exc.value.code == SESSION_NOT_FOUND


# 功能：验证 closed session 不能继续 send_message
# 设计：先显式 close，再发送消息，断言 session_closed 错误码，覆盖状态机拒绝路径
async def test_closed_session_rejects_message(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    await manager.close(session.id)

    with pytest.raises(HandlerError) as exc:
        await manager.send_message(session.id, "again")
    assert exc.value.code == SESSION_CLOSED


# 功能：验证手动 /skill 触发时 thread 存一条 user 消息（告知 skill 名 + 路径 + 任务），不含原始 / 命令
# 设计：在 .weave/skills 写 explain skill 并 chdir 到该目录，send_message("/explain 解析loader.py")，
#       断言 thread 里是 user_prompt（skill 名 + 路径 + 任务），不含 skill 正文（agent 用 read_file 读）
async def test_skill_invocation_writes_user_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_dir = tmp_path / ".weave" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "explain.md").write_text(
        "---\nname: explain\ndescription: 解释代码\n---\n你是一位代码讲解员。\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    store = await _store(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    await manager.send_message(session.id, "/explain 解析loader.py")

    messages = await store.read_messages(session.id)
    assert messages[0]["role"] == "user"
    assert "用户想要使用 skill：explain" in messages[0]["content"]
    assert f"skill.md 路径：{(skills_dir / 'explain.md').resolve()}" in messages[0]["content"]
    assert "用户任务：解析loader.py" in messages[0]["content"]
    # 不注入 skill 正文（agent 按系统提示规则用 read_file 读取）
    assert "你是一位代码讲解员。" not in messages[0]["content"]


# 功能：验证 create 传入的 working_dir 会写入内存 session 与磁盘 meta.json
# 设计：create 时指定 cwd，断言内存对象与持久化 meta 都保存该工作目录（会话级工作目录的基础）
async def test_create_session_persists_working_dir(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]

    session = await manager.create("chat", "t", working_dir="/proj/x")

    assert session.working_dir == "/proj/x"
    assert (await store.read_meta(session.id)).working_dir == "/proj/x"


# 功能：验证未指定 working_dir 时回退到进程 cwd
# 设计：不带 working_dir 创建会话，断言 working_dir 为空串且 effective_working_dir 回退到 Path.cwd()
async def test_effective_working_dir_falls_back_to_cwd(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]

    session = await manager.create("chat", "t")

    assert session.working_dir == ""
    assert session.effective_working_dir() == str(Path.cwd())
