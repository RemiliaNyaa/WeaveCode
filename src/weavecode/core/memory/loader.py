from __future__ import annotations

from pathlib import Path

GLOBAL_CONTEXT_PATH = "~/.weave/context.md"
PROJECT_CONTEXT_RELATIVE = ".weave/context.md"


# 读取指定路径的上下文文件；路径不存在或不是文件时返回空字符串
def load_context_file(path: Path) -> str:
    p = path.expanduser()
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8").strip()


# 读取全局上下文文件的绝对路径
def global_context_path() -> Path:
    return Path(GLOBAL_CONTEXT_PATH).expanduser()


# 读取指定工作目录下的项目上下文文件路径；未指定工作目录时相对当前目录
def project_context_path(working_dir: str = "") -> Path:
    if working_dir:
        return Path(working_dir) / PROJECT_CONTEXT_RELATIVE
    return Path(PROJECT_CONTEXT_RELATIVE)


# 按「全局 → 项目」的顺序加载两级上下文，供系统提示分层拼接
def load_contexts(working_dir: str = "") -> tuple[str, str]:
    global_text = load_context_file(global_context_path())
    project_text = load_context_file(project_context_path(working_dir))
    return global_text, project_text
