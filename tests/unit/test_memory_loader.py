from __future__ import annotations

from pathlib import Path

from weavecode.core.memory.loader import load_context_file


# 功能：验证文件存在时返回去除首尾空格的完整内容
# 设计：用 tmp_path 写入带前后空白行的文件，断言 strip 后内容一致
def test_load_existing_file(tmp_path: Path) -> None:
    rules = tmp_path / "context.md"
    rules.write_text("  # My Rules\n- item one\n", encoding="utf-8")
    assert load_context_file(rules) == "# My Rules\n- item one"


# 功能：验证文件不存在时返回空字符串
# 设计：传入不存在的路径，无需创建文件，断言返回值为空字符串
def test_load_missing_file(tmp_path: Path) -> None:
    assert load_context_file(tmp_path / "nonexistent.md") == ""


# 功能：验证文件存在但内容为空（或仅空白）时返回空字符串
# 设计：写入纯空白内容，strip 后为空，断言返回空字符串
def test_load_empty_file(tmp_path: Path) -> None:
    rules = tmp_path / "context.md"
    rules.write_text("   \n\n  ", encoding="utf-8")
    assert load_context_file(rules) == ""
