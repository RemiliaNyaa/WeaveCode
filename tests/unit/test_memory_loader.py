from __future__ import annotations

from pathlib import Path

from weavecode.core.memory.loader import (
    MAX_RULE_BYTES,
    MAX_RULE_LINES,
    PREVIEW_LINES,
    cap_rules,
    load_rules_file,
    preview_rules,
)


# 功能：验证文件存在时返回去除首尾空格的完整内容
# 设计：用 tmp_path 写入带前后空白行的文件，断言 strip 后内容一致
def test_load_existing_file(tmp_path: Path) -> None:
    rules = tmp_path / "AGENTS.md"
    rules.write_text("  # My Rules\n- item one\n", encoding="utf-8")
    assert load_rules_file(rules) == "# My Rules\n- item one"


# 功能：验证文件不存在时返回空字符串
# 设计：传入不存在的路径，无需创建文件，断言返回值为空字符串
def test_load_missing_file(tmp_path: Path) -> None:
    assert load_rules_file(tmp_path / "nonexistent.md") == ""


# 功能：验证文件存在但内容为空（或仅空白）时返回空字符串
# 设计：写入纯空白内容，strip 后为空，断言返回空字符串
def test_load_empty_file(tmp_path: Path) -> None:
    rules = tmp_path / "AGENTS.md"
    rules.write_text("   \n\n  ", encoding="utf-8")
    assert load_rules_file(rules) == ""


# 功能：验证超过 500 行时按行数上限截断
# 设计：写入 600 行，断言结果恰好 500 行且保留的是前面的内容
def test_load_truncates_at_line_limit(tmp_path: Path) -> None:
    rules = tmp_path / "AGENTS.md"
    rules.write_text("\n".join(f"line-{i}" for i in range(600)), encoding="utf-8")

    result = load_rules_file(rules)

    assert len(result.splitlines()) == MAX_RULE_LINES
    assert result.startswith("line-0")
    assert "line-599" not in result


# 功能：验证行数不超但字节超限时按字节上限截断
# 设计：10 行长行（远超 32 KiB），断言结果字节数不超过上限
def test_load_truncates_at_byte_limit(tmp_path: Path) -> None:
    rules = tmp_path / "AGENTS.md"
    rules.write_text("\n".join("x" * 4000 for _ in range(10)), encoding="utf-8")

    result = load_rules_file(rules)

    assert len(result.encode("utf-8")) <= MAX_RULE_BYTES


# 功能：验证 cap_rules 对未超限文本原样返回
# 设计：短文本走两个上限都不触发，断言内容不变
def test_cap_rules_passthrough() -> None:
    assert cap_rules("hello\nworld") == "hello\nworld"


# 功能：验证 preview_rules 对不存在的文件返回 exists=False 且不抛异常
# 设计：传一个不存在的路径，断言各统计字段为 0
def test_preview_missing_file(tmp_path: Path) -> None:
    preview = preview_rules(tmp_path / "AGENTS.md", "project")

    assert preview.exists is False
    assert preview.scope == "project"
    assert preview.total_lines == 0
    assert preview.content == ""


# 功能：验证 preview_rules 统计行数/字节数，小文件不截断
# 设计：3 行短内容，断言 truncated=False 且行数统计正确
def test_preview_small_file(tmp_path: Path) -> None:
    rules = tmp_path / "AGENTS.md"
    rules.write_text("a\nb\nc\n", encoding="utf-8")

    preview = preview_rules(rules, "global")

    assert preview.exists is True
    assert preview.scope == "global"
    assert preview.total_lines == 3
    assert preview.truncated is False
    assert preview.content == "a\nb\nc\n"


# 功能：验证超过预览上限时置 truncated 并只给前 100 行
# 设计：写入 150 行，断言 truncated=True、shown_lines=100、总行数仍是 150
def test_preview_truncates_at_preview_limit(tmp_path: Path) -> None:
    rules = tmp_path / "AGENTS.md"
    rules.write_text("\n".join(f"l{i}" for i in range(150)), encoding="utf-8")

    preview = preview_rules(rules, "project")

    assert preview.truncated is True
    assert preview.shown_lines == PREVIEW_LINES
    assert preview.total_lines == 150
    assert preview.content.startswith("l0")
