from __future__ import annotations

from pathlib import Path

from weavecode.core.tools.builtin.grep import GrepTool


# 功能：验证正则搜索命中时返回分组格式（文件路径 + 缩进 Line N）
# 设计：写含关键字文件，断言首行 Found 1 matches、路径行、缩进行，覆盖方案A输出结构
async def test_grep_basic_match(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("def foo():\n    return 42\n", encoding="utf-8")
    result = await GrepTool().invoke({"pattern": "return", "path": str(tmp_path)})
    assert not result.is_error
    lines = result.content.splitlines()
    assert lines[0] == "Found 1 matches"
    assert lines[1] == f"{f}:"
    assert lines[2] == "  Line 2:     return 42"


# 功能：验证同一文件多处命中合并到同一个路径标题下，不同文件之间用空行分隔
# 设计：两个文件各含多处匹配，断言路径标题不重复且文件间有空行，覆盖分组去重
async def test_grep_groups_by_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\ny\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x\n", encoding="utf-8")
    result = await GrepTool().invoke({"pattern": "^[xy]$", "path": str(tmp_path)})
    assert not result.is_error
    assert result.content.count(f"{tmp_path / 'a.py'}:") == 1
    assert result.content.count(f"{tmp_path / 'b.py'}:") == 1
    assert "\n\n" in result.content  # 文件间空行


# 功能：验证相对路径被拒绝（只支持绝对路径）
# 设计：传相对路径，断言 is_error 且消息含 Invalid path，覆盖绝对路径约束
async def test_grep_relative_path_rejected(tmp_path: Path) -> None:
    result = await GrepTool().invoke({"pattern": "x", "path": "."})
    assert result.is_error
    assert "Invalid path" in result.content


# 功能：验证不存在的路径返回错误
# 设计：传 tmp_path 下不存在的路径，断言 is_error 且消息含 Path not found
async def test_grep_missing_path(tmp_path: Path) -> None:
    result = await GrepTool().invoke({"pattern": "x", "path": str(tmp_path / "nope")})
    assert result.is_error
    assert "Path not found" in result.content


# 功能：验证非法正则返回错误而非崩溃
# 设计：传未闭合括号 "("，断言 is_error 且消息含 Invalid regex pattern
async def test_grep_invalid_regex(tmp_path: Path) -> None:
    result = await GrepTool().invoke({"pattern": "(", "path": str(tmp_path)})
    assert result.is_error
    assert "Invalid regex pattern" in result.content


# 功能：验证无匹配时返回 No files found
# 设计：目录内有文件但模式不命中，断言返回固定提示文本
async def test_grep_no_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    result = await GrepTool().invoke({"pattern": "zzz", "path": str(tmp_path)})
    assert not result.is_error
    assert result.content == "No files found"


# 功能：验证 include 按文件名模式过滤搜索范围
# 设计：同目录放 .py 与 .md 各含关键字，include="*.py" 时只命中 .py
async def test_grep_include_filter(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("needle\n", encoding="utf-8")
    result = await GrepTool().invoke({"pattern": "needle", "path": str(tmp_path), "include": "*.py"})
    assert not result.is_error
    assert "a.py" in result.content
    assert "b.md" not in result.content


# 功能：验证 include 花括号模式展开（*.{py,ts} 同时命中两类文件）
# 设计：py 与 ts 各命中一次，断言两类文件都在结果里，覆盖 brace 展开
async def test_grep_include_braces(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "b.ts").write_text("needle\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("needle\n", encoding="utf-8")
    result = await GrepTool().invoke({"pattern": "needle", "path": str(tmp_path), "include": "*.{py,ts}"})
    assert not result.is_error
    assert "a.py" in result.content
    assert "b.ts" in result.content
    assert "c.md" not in result.content


# 功能：验证二进制文件被跳过（内容含 NUL 字节）
# 设计：写一个含 \x00 的"文本"，断言即使其内容含关键字也不出现在结果中
async def test_grep_skips_binary(tmp_path: Path) -> None:
    (tmp_path / "bin.py").write_bytes(b"needle\x00tail")
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    result = await GrepTool().invoke({"pattern": "needle", "path": str(tmp_path)})
    assert not result.is_error
    assert "a.py" in result.content
    assert "bin.py" not in result.content


# 功能：验证常见工具/依赖目录被忽略
# 设计：node_modules 内含命中文件，断言其结果不出现在输出里
async def test_grep_skips_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("needle\n", encoding="utf-8")
    result = await GrepTool().invoke({"pattern": "needle", "path": str(tmp_path)})
    assert not result.is_error
    assert "ok.py" in result.content
    assert "dep.py" not in result.content


# 功能：验证 path 指向单个文件时只搜该文件
# 设计：目录下放两个文件，path 指定其中一个，断言只命中目标文件
async def test_grep_single_file_path(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_text("needle\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("needle\n", encoding="utf-8")
    result = await GrepTool().invoke({"pattern": "needle", "path": str(target)})
    assert not result.is_error
    assert "target.py" in result.content
    assert "other.py" not in result.content


# 功能：验证超过 100 条时截断并附提示
# 设计：一个文件写 120 行均命中，断言恰好 100 条 + 截断提示，覆盖 limit 截断行为
async def test_grep_truncates_at_limit(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"match{i}" for i in range(120)) + "\n", encoding="utf-8")
    result = await GrepTool().invoke({"pattern": "match", "path": str(f)})
    assert not result.is_error
    assert result.content.count("  Line ") == 100
    assert "more matches available" in result.content
    assert "Results truncated" in result.content