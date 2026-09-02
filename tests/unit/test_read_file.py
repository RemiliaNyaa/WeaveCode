from __future__ import annotations

from pathlib import Path

import pytest

from weavecode.core.tools.builtin.read_file import ReadFileTool


# 功能：验证读取存在的文件时返回完整内容且 is_error 为 False
# 设计：写临时文件后读取，断言 content 和 is_error，覆盖正常路径（happy path）
async def test_read_existing_file(tmp_path: Path) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("hello world", encoding="utf-8")
    result = await ReadFileTool().invoke({"path": str(f)})
    assert not result.is_error
    assert result.content == "1: hello world\n\n(End of file - total 1 lines)"


# 功能：验证相对路径被拒绝（只支持绝对路径）
# 设计：传相对路径，断言 is_error 且消息含 Invalid path / absolute，覆盖对齐 OpenCode 的绝对路径约束
async def test_relative_path_rejected(tmp_path: Path) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("hello", encoding="utf-8")
    result = await ReadFileTool().invoke({"path": "hello.txt"})
    assert result.is_error
    assert "Invalid path" in result.content
    assert "absolute path" in result.content


# 功能：验证文件不存在时抛 FileNotFoundError 而非返回错误 ToolResult
# 设计：传入不存在的路径，确认 ReadFileTool 不吞掉异常，让调用方（invoke_tool）负责错误分类和事件发布
async def test_file_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await ReadFileTool().invoke({"path": str(tmp_path / "missing.txt")})


# 功能：验证文件不存在且同目录有相似文件名时，异常消息附带 Did you mean 建议
# 设计：写相似文件后读不存在的目标，断言消息含建议路径，覆盖"记错文件名前缀/后缀"场景
async def test_file_not_found_suggests_similar(tmp_path: Path) -> None:
    (tmp_path / "my_config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError) as exc:
        await ReadFileTool().invoke({"path": str(tmp_path / "my_config.json.bak")})
    assert "Did you mean one of these?" in str(exc.value)
    assert "my_config.json" in str(exc.value)


# 功能：验证每行内容带行号前缀
# 设计：写 3 行文件，断言三行分别以 1:/2:/3: 开头，覆盖行号标注
async def test_lines_are_numbered(tmp_path: Path) -> None:
    f = tmp_path / "nums.txt"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    result = await ReadFileTool().invoke({"path": str(f)})
    assert not result.is_error
    assert "1: a" in result.content
    assert "2: b" in result.content
    assert "3: c" in result.content


# 功能：验证 offset 从指定行开始读（1-indexed）
# 设计：写 5 行文件，offset=3 断言从第 3 行开始且首行标号为 3
async def test_offset_starts_at_line(tmp_path: Path) -> None:
    f = tmp_path / "five.txt"
    f.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
    result = await ReadFileTool().invoke({"path": str(f), "offset": 3})
    assert not result.is_error
    assert result.content.startswith("3: l3")
    assert "2: l2" not in result.content
    assert "1: l1" not in result.content


# 功能：验证 limit 限制读取行数并在末尾提示可继续读
# 设计：写 5 行文件，limit=2 断言只读 2 行且 footer 提示 Showing/offset
async def test_limit_restricts_lines(tmp_path: Path) -> None:
    f = tmp_path / "five.txt"
    f.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
    result = await ReadFileTool().invoke({"path": str(f), "limit": 2})
    assert not result.is_error
    assert "1: l1" in result.content
    assert "2: l2" in result.content
    assert "3: l3" not in result.content
    assert "(Showing lines 1-2 of 5. Use offset=3 to continue.)" in result.content


# 功能：验证 offset 越界时返回错误 ToolResult
# 设计：5 行文件 offset=10，断言 is_error 且消息含 out of range
async def test_offset_out_of_range(tmp_path: Path) -> None:
    f = tmp_path / "five.txt"
    f.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
    result = await ReadFileTool().invoke({"path": str(f), "offset": 10})
    assert result.is_error
    assert "Offset 10 is out of range for this file (5 lines)" in result.content


# 功能：验证二进制文件返回不支持类型提示而非乱码
# 设计：写带 NUL 字节的文件，断言 is_error 且含 Unsupported file type
async def test_binary_file_returns_unsupported(tmp_path: Path) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"\x00\x01\x02\x03hello")
    result = await ReadFileTool().invoke({"path": str(f)})
    assert result.is_error
    assert "Unsupported file type" in result.content


# 功能：验证图片扩展名文件返回不支持类型提示
# 设计：写 .png 文件，断言 is_error 且含 Unsupported file type（扩展名命中）
async def test_image_file_returns_unsupported(tmp_path: Path) -> None:
    f = tmp_path / "pic.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    result = await ReadFileTool().invoke({"path": str(f)})
    assert result.is_error
    assert "Unsupported file type" in result.content


# 功能：验证空文件返回空内容（无行号、无 footer 追加行）
# 设计：零字节文件确认 content 不含行号和 footer，正常返回
async def test_empty_file_returns_empty_content(tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")
    result = await ReadFileTool().invoke({"path": str(f)})
    assert not result.is_error
    assert result.content == ""


# 功能：验证超过 50KB 输出被截断且末尾提示继续读取
# 设计：写大量文本行使累计字节超 50KB，断言末尾含 Output capped 提示
async def test_byte_limit_truncates(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("\n".join(["x" * 100] * 600), encoding="utf-8")  # ~60KB
    result = await ReadFileTool().invoke({"path": str(f)})
    assert not result.is_error
    assert "Output capped at 50 KB" in result.content
    assert "(End of file" not in result.content