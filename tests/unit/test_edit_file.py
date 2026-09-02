from __future__ import annotations

from pathlib import Path

from weavecode.core.tools.builtin.edit_file import EditFileTool


# 功能：验证 edit_file 精确替换匹配到的唯一文本
# 设计：写入含 "foo" 的文件，替换 "foo"→"bar"，断言内容变化且返回成功
async def test_edit_single_match(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("foo baz", encoding="utf-8")
    result = await EditFileTool().invoke(
        {"path": str(f), "old_string": "foo", "new_string": "bar"}
    )
    assert not result.is_error
    assert f.read_text() == "bar baz"
    assert "Edit applied successfully" in result.content


# 功能：验证多匹配时替换第一处，并在结果中说明匹配总数
# 设计：文件含两处 "foo"，断言只改第一处，且内容含 "2 locations"
async def test_edit_multiple_matches_replaces_first(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("foo foo bar", encoding="utf-8")
    result = await EditFileTool().invoke(
        {"path": str(f), "old_string": "foo", "new_string": "bar"}
    )
    assert not result.is_error
    assert f.read_text() == "bar foo bar"
    assert "2 locations" in result.content


# 功能：验证 old_string 找不到时报错并提示精确匹配
# 设计：传入不存在的文本，断言 is_error 且消息含 "match exactly"
async def test_edit_not_found(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    result = await EditFileTool().invoke(
        {"path": str(f), "old_string": "zzz", "new_string": "yyy"}
    )
    assert result.is_error
    assert "Could not find old_string" in result.content
    assert "match exactly" in result.content
    assert f.read_text() == "hello"


# 功能：验证 old_string 为空时报错
# 设计：空串可匹配任意位置（等同任意插入），必须拒绝并引导使用 write_file
async def test_edit_empty_old_string(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    result = await EditFileTool().invoke(
        {"path": str(f), "old_string": "", "new_string": "x"}
    )
    assert result.is_error
    assert "cannot be empty" in result.content
    assert "write_file" in result.content


# 功能：验证新旧字符串相同返回"无事发生"，且文件未被改动
# 设计：old == new 直接返回非错误提示，无需读取文件做任何替换
async def test_edit_identical_strings(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    result = await EditFileTool().invoke(
        {"path": str(f), "old_string": "hello", "new_string": "hello"}
    )
    assert not result.is_error
    assert "identical" in result.content
    assert f.read_text() == "hello"


# 功能：验证文件不存在时报错并引导使用 write_file
# 设计：目标路径不存在，断言 is_error 且消息提示"只编辑已存在文件"
async def test_edit_file_not_found(tmp_path: Path) -> None:
    result = await EditFileTool().invoke(
        {"path": str(tmp_path / "nope.txt"), "old_string": "a", "new_string": "b"}
    )
    assert result.is_error
    assert "File not found" in result.content
    assert "write_file" in result.content


# 功能：验证目录路径报错（edit 不能作用于目录）
# 设计：传入目录路径，断言 is_error 且消息含 "directory"
async def test_edit_directory_rejected(tmp_path: Path) -> None:
    result = await EditFileTool().invoke(
        {"path": str(tmp_path), "old_string": "a", "new_string": "b"}
    )
    assert result.is_error
    assert "directory" in result.content


# 功能：验证相对路径被拒绝（只支持绝对路径）
# 设计：传相对路径，断言 is_error 且消息含 Invalid path / absolute
async def test_edit_relative_path_rejected(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    result = await EditFileTool().invoke({"path": "a.txt", "old_string": "x", "new_string": "y"})
    assert result.is_error
    assert "Invalid path" in result.content
    assert "absolute path" in result.content


# 功能：验证 CRLF 行尾文件也能被 LF 格式的 old_string 匹配（行尾归一化）
# 设计：文件用 \r\n 写入，old_string 用 \n，断言替换成功且行尾保持 CRLF
async def test_edit_matches_crlf_file_with_lf_old_string(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_bytes(b"foo\r\nbar\r\n")
    result = await EditFileTool().invoke(
        {"path": str(f), "old_string": "foo\n", "new_string": "baz\n"}
    )
    assert not result.is_error
    assert f.read_bytes() == b"baz\r\nbar\r\n"


# 功能：验证多行 old_string 的精确匹配（含换行与缩进）
# 设计：old_string 带 \n 和 4 空格缩进，替换多行块，断言整体替换成功
async def test_edit_multiline_exact_match(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("def f():\n    x = 1\n    return x\n", encoding="utf-8")
    result = await EditFileTool().invoke(
        {
            "path": str(f),
            "old_string": "    x = 1\n    return x",
            "new_string": "    x = 2\n    return x * 2",
        }
    )
    assert not result.is_error
    assert f.read_text() == "def f():\n    x = 2\n    return x * 2\n"


# 功能：验证空文件编辑时报错（空串被拒绝，且空文件无可替换内容）
# 设计：空文件内找不到任何非空 old_string，断言 not found 类错误
async def test_edit_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("", encoding="utf-8")
    result = await EditFileTool().invoke(
        {"path": str(f), "old_string": "x", "new_string": "y"}
    )
    assert result.is_error
    assert "Could not find old_string" in result.content