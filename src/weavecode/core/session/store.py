from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weavecode.core.session.model import Session

logger = logging.getLogger(__name__)

MessageContent = str | list[dict[str, Any]]

_DEFAULT_ROOT = "~/.weave/sessions"


class SessionStore:
    # 初始化会话存储：根目录默认 ~/.weave/sessions，不存在则创建
    def __init__(self, root: str | Path | None = None) -> None:
        self._root = (
            Path(root).expanduser() if root is not None else Path(_DEFAULT_ROOT).expanduser()
        )
        self._root.mkdir(parents=True, exist_ok=True)

    # 会话目录：<root>/<sid>
    def session_dir(self, sid: str) -> Path:
        return self._root / sid

    # 会话下的 run 目录：<root>/<sid>/runs
    def runs_dir(self, sid: str) -> Path:
        return self.session_dir(sid) / "runs"

    # 为指定 run 建目录并返回路径，事件流与任务文件都写在里面
    def ensure_run_dir(self, sid: str, run_id: str) -> Path:
        path = self.runs_dir(sid) / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    # 将 session meta 写入 meta.json
    def write_meta(self, session: Session) -> None:
        directory = self.session_dir(session.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "meta.json").write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # 读取 meta.json；不存在时抛 FileNotFoundError
    def read_meta(self, sid: str) -> Session:
        path = self.session_dir(sid) / "meta.json"
        if not path.exists():
            raise FileNotFoundError(f"session not found: {sid}")
        return Session.from_dict(json.loads(path.read_text(encoding="utf-8")))

    # 追加一条消息
    def append_message(
        self, sid: str, role: str, content: MessageContent, run_id: str | None = None
    ) -> None:
        self.append_messages(sid, [{"role": role, "content": content}], run_id=run_id or "")

    # 批量追加一次 run 新产生的消息，按行写入 thread.jsonl
    def append_messages(self, sid: str, messages: list[dict[str, Any]], run_id: str = "") -> None:
        directory = self.session_dir(sid)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "thread.jsonl").open("a", encoding="utf-8") as fh:
            for msg in messages:
                row: dict[str, Any] = {
                    "role": msg.get("role", ""),
                    "content": msg.get("content", ""),
                }
                if run_id:
                    row["run_id"] = run_id
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 读取完整 thread 并拼回可直接传给 Anthropic 的 messages
    def read_messages(self, sid: str) -> list[dict[str, Any]]:
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return []

        messages: list[dict[str, Any]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skip broken thread row sid=%s line=%s", sid, line_no)
                continue
            role = row.get("role")
            if role not in ("user", "assistant"):
                continue
            messages.append({"role": role, "content": row.get("content", "")})
        return self._trim_orphan_tool_use(messages)

    # 裁掉尾部未配对 tool_use 以及其后的消息，避免 Anthropic messages.invalid
    def _trim_orphan_tool_use(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: set[str] = set()
        last_balanced = 0
        for idx, msg in enumerate(messages, start=1):
            content = msg.get("content")
            if isinstance(content, list):
                if msg.get("role") == "assistant":
                    for block in content:
                        if block.get("type") == "tool_use":
                            pending.add(str(block.get("id", "")))
                elif msg.get("role") == "user":
                    for block in content:
                        if block.get("type") == "tool_result":
                            pending.discard(str(block.get("tool_use_id", "")))
            if not pending:
                last_balanced = idx
        if pending:
            logger.warning("trim orphan tool_use blocks from thread")
            return messages[:last_balanced]
        return messages

    # 读取会话笔记；文件不存在时返回空字符串
    def read_notes(self, sid: str) -> str:
        path = self.session_dir(sid) / "notes.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    # 追加一条会话笔记：带时间戳与 run 归属的小节，写进 notes.md
    def append_note(self, sid: str, title: str, content: str, run_id: str = "") -> None:
        directory = self.session_dir(sid)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat()
        attribution = f"<!-- {stamp}" + (f" run={run_id}" if run_id else "") + " -->"
        heading = title.strip() or "note"
        with (directory / "notes.md").open("a", encoding="utf-8") as fh:
            fh.write(f"\n{attribution}\n## {heading}\n\n{content.strip()}\n")
