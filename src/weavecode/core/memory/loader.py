from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 注入给模型的单份 AGENTS.md 上限：最多 500 行，且最多 32 KiB（后者防单行超长）
MAX_RULE_LINES = 500
MAX_RULE_BYTES = 32 * 1024

# `/rules` 预览上限：最多 100 行，且最多 8 KiB
PREVIEW_LINES = 100
PREVIEW_BYTES = 8 * 1024

GLOBAL_RULES_PATH = "~/.weave/AGENTS.md"
PROJECT_RULES_RELATIVE = ".weave/AGENTS.md"


# 按「行数上限 → 字节上限」依次截断
def _cap(text: str, max_lines: int, max_bytes: int) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines])
    raw = text.encode("utf-8")
    if len(raw) > max_bytes:
        # errors="ignore" 兜住被切断的多字节字符
        text = raw[:max_bytes].decode("utf-8", errors="ignore")
    return text


# 读取指定路径的 AGENTS.md；路径不存在或内容为空时返回空字符串
def load_rules_file(path: Path) -> str:
    p = path.expanduser()
    if not p.exists():
        return ""
    return _cap(p.read_text(encoding="utf-8"), MAX_RULE_LINES, MAX_RULE_BYTES).strip()


# 按注入上限截断（截断不追加提示，对齐 codex 的静默截断）
def cap_rules(text: str) -> str:
    return _cap(text, MAX_RULE_LINES, MAX_RULE_BYTES)


@dataclass(frozen=True)
class RulesPreview:
    scope: str          # "global" | "project"
    path: str
    exists: bool
    total_lines: int
    total_bytes: int
    shown_lines: int
    content: str        # 已按预览上限截断的内容
    truncated: bool


# 读取全局规则文件的绝对路径
def global_rules_path() -> Path:
    return Path(GLOBAL_RULES_PATH).expanduser()


# 读取指定工作目录下的项目规则文件路径
def project_rules_path(working_dir: str) -> Path:
    return Path(working_dir) / PROJECT_RULES_RELATIVE


# 生成一份规则文件的预览；文件不存在或读不到时 exists=False，不抛异常
def preview_rules(path: Path, scope: str) -> RulesPreview:
    p = path.expanduser()
    empty = RulesPreview(
        scope=scope, path=str(p), exists=False, total_lines=0, total_bytes=0,
        shown_lines=0, content="", truncated=False,
    )
    if not p.is_file():
        return empty
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return empty
    shown = _cap(text, PREVIEW_LINES, PREVIEW_BYTES)
    return RulesPreview(
        scope=scope,
        path=str(p),
        exists=True,
        total_lines=len(text.splitlines()),
        total_bytes=len(text.encode("utf-8")),
        shown_lines=len(shown.splitlines()),
        content=shown,
        truncated=shown != text,
    )
