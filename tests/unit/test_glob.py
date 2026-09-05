from __future__ import annotations

from pathlib import Path

from weavecode.core.tools.builtin.glob import GlobTool


# 功能：验证 glob 递归匹配文件名并返回绝对路径列表
# 设计：建顶层与嵌套两级文件，断言两个 .py 都在结果中且以绝对路径形式出现，覆盖递归匹配
async def test_glob_recursive_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("", encoding="utf-8")
    result = await GlobTool().invoke({"pattern": "**/*.py", "path": str(tmp_path)})
    assert not result.is_error
    lines = result.content.splitlines()
    assert str(tmp_path / "a.py") in lines
    assert str(tmp_path / "sub" / "b.py") in lines


# 功能：验证普通 basename 模式（*.py）也能递归命中任意深度的文件
# 设计：嵌套目录下建 .py，用无目录部分的模式匹配，覆盖 Path.match 对 basename 的匹配语义
async def test_glob_basename_pattern_recursive(tmp_path: Path) -> None:
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "c.py").write_text("", encoding="utf-8")
    result = await GlobTool().invoke({"pattern": "*.py", "path": str(tmp_path)})
    assert not result.is_error
    assert str(tmp_path / "deep" / "c.py") in result.content


# 功能：验证相对路径被拒绝（只支持绝对路径）
# 设计：传相对路径，断言 is_error 且消息含 Invalid path，覆盖对齐 read_file 的绝对路径约束
async def test_glob_relative_path_rejected(tmp_path: Path) -> None:
    result = await GlobTool().invoke({"pattern": "*.py", "path": "."})
    assert result.is_error
    assert "Invalid path" in result.content
    assert "absolute path" in result.content


# 功能：验证不存在的目录返回错误而非崩溃
# 设计：传 tmp_path 下不存在的子目录，断言 is_error 且消息含 Directory not found
async def test_glob_missing_dir(tmp_path: Path) -> None:
    result = await GlobTool().invoke({"pattern": "*.py", "path": str(tmp_path / "nope")})
    assert result.is_error
    assert "Directory not found" in result.content


# 功能：验证 path 指向文件而非目录时返回错误
# 设计：建一个普通文件当 path，断言 is_error 且消息含 Not a directory
async def test_glob_path_is_file(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("", encoding="utf-8")
    result = await GlobTool().invoke({"pattern": "*.py", "path": str(f)})
    assert result.is_error
    assert "Not a directory" in result.content


# 功能：验证无匹配时返回 No files found
# 设计：目录内只有 .py，用 .md 模式搜索，断言返回固定提示文本
async def test_glob_no_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    result = await GlobTool().invoke({"pattern": "*.md", "path": str(tmp_path)})
    assert not result.is_error
    assert result.content == "No files found"


# 功能：验证常见的工具/依赖目录被忽略（.git、node_modules 等）
# 设计：在 node_modules 里建 .py，断言 glob 结果不含该文件，覆盖忽略目录剪枝
async def test_glob_skips_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("", encoding="utf-8")
    result = await GlobTool().invoke({"pattern": "**/*.py", "path": str(tmp_path)})
    assert not result.is_error
    assert str(tmp_path / "ok.py") in result.content
    assert "dep.py" not in result.content


# 功能：验证超过 100 条时截断并附提示
# 设计：生成 105 个 .txt，断言恰好 100 条 + 截断提示行，覆盖 OpenCode 的 limit 截断行为
async def test_glob_truncates_at_limit(tmp_path: Path) -> None:
    for i in range(105):
        (tmp_path / f"f{i:03}.txt").write_text("", encoding="utf-8")
    result = await GlobTool().invoke({"pattern": "*.txt", "path": str(tmp_path)})
    assert not result.is_error
    assert len(result.content.splitlines()) == 102  # 100 条 + 空行 + 提示
    assert "Results are truncated" in result.content
    assert "showing first 100 results" in result.content