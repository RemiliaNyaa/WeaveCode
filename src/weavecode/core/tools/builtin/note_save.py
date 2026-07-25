from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from weavecode.core.tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from weavecode.core.session.store import SessionStore


class NoteSaveParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str
    content: str


class NoteSaveTool(BaseTool):
    """会话笔记工具：把模型给出的笔记追加写入当前会话的 notes 文件。"""

    params_model = NoteSaveParams
    name = "note_save"
    description = (
        "Save a fact or decision to this session's notes.\n"
        "Notes stay with the session and are visible to you in later turns.\n"
        "\n"
        "Rules:\n"
        "- title is a short heading for the note.\n"
        "- content holds the fact or decision worth remembering."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short heading of the note."},
            "content": {"type": "string", "description": "Fact or decision to remember."},
        },
        "required": ["title", "content"],
    }

    # 注入会话存储与本次 run 标识：笔记写进当前会话目录，读回时统一走会话存储
    def __init__(self, store: SessionStore, session_id: str, run_id: str = "") -> None:
        self._store = store
        self._session_id = session_id
        self._run_id = run_id

    # 追加一条笔记；正文为空按参数错误回填，不写文件
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = NoteSaveParams.model_validate(params)
        content = p.content.strip()
        if not content:
            return ToolResult(
                content="content must not be empty",
                is_error=True,
                error_type="schema_error",
            )
        self._store.append_note(self._session_id, p.title, content, self._run_id)
        return ToolResult(content="saved")
