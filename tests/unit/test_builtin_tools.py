from __future__ import annotations

from pathlib import Path

import pytest

from weavecode.core.tools.builtin.bash import BashTool
from weavecode.core.tools.builtin.list_dir import ListDirTool
from weavecode.core.tools.builtin.write_file import WriteFileTool

# ── bash ──────────────────────────────────────────────────────────────────────

# 功能：验证成功命令的 stdout 出现在 ToolResult.content 中，is_error 为 False
# 设计：用 echo 命令避免外部依赖，直接比较输出内容，无需 mock
@pytest.mark.asyncio
async def test_bash_success_stdout() -> None:
    result = await BashTool().invoke({"command": "echo hello"})
    assert not result.is_error
    assert "hello" in result.content


# 功能：验证非零退出码时 is_error=True 且 content 包含退出码标注
# 设计：`exit 2` 是最简单的非零退出；不依赖任何外部命令行为
@pytest.mark.asyncio
async def test_bash_nonzero_exit_is_error() -> None:
    result = await BashTool().invoke({"command": "exit 2"})
    assert result.is_error
    assert "[exit 2]" in result.content


# 功能：验证命令超时后 is_error=True，error_type 为 "timeout"
# 设计：timeout=1s 搭配 sleep 2 必然超时；验证 error_type 而非 content，避免超时消息格式耦合
@pytest.mark.asyncio
async def test_bash_timeout() -> None:
    result = await BashTool().invoke({"command": "sleep 5", "timeout": 1})
    assert result.is_error
    assert result.error_type == "timeout"


# 功能：验证 stderr 被合并到 stdout 输出中
# 设计：只写 stderr 的命令（>&2 echo），输出应该出现在合并后的 content 里
@pytest.mark.asyncio
async def test_bash_stderr_merged() -> None:
    result = await BashTool().invoke({"command": "echo err >&2"})
    assert not result.is_error
    assert "err" in result.content


# ── write_file ────────────────────────────────────────────────────────────────

# 功能：验证 write_file 写入文件后内容可以被读取，返回字节数
# 设计：写入临时目录，断言文件存在且内容一致；用 tmp_path fixture 自动清理
@pytest.mark.asyncio
async def test_write_file_creates_and_returns_size(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    result = await WriteFileTool().invoke(
        {"path": str(target), "content": "hello world"}
    )
    assert not result.is_error
    assert "11" in result.content  # "hello world" = 11 bytes
    assert target.read_text() == "hello world"


# 功能：验证 write_file 自动创建不存在的父目录
# 设计：路径包含两层不存在的子目录，确认写入后目录结构被创建
@pytest.mark.asyncio
async def test_write_file_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "file.txt"
    result = await WriteFileTool().invoke({"path": str(target), "content": "x"})
    assert not result.is_error
    assert target.exists()


# ── list_dir ──────────────────────────────────────────────────────────────────

# 功能：验证 list_dir 输出包含目录直接子项，目录加 / 后缀
# 设计：在 tmp_path 创建文件和子目录，断言文件名出现、目录名带 /；平铺不递归
@pytest.mark.asyncio
async def test_list_dir_shows_files(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("x")
    (tmp_path / "bar.md").write_text("y")
    (tmp_path / "subdir").mkdir()
    result = await ListDirTool().invoke({"path": str(tmp_path)})
    assert not result.is_error
    assert "foo.py" in result.content
    assert "bar.md" in result.content
    assert "subdir/" in result.content


# 功能：验证 list_dir 平铺不递归（子目录里的内容不出现）
# 设计：创建 child/grandchild 两层，断言只显示 child/ 而不显示孙级文件
@pytest.mark.asyncio
async def test_list_dir_no_recursion(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    grandchild = child / "grandchild"
    grandchild.mkdir()
    (grandchild / "deep.txt").write_text("x")

    result = await ListDirTool().invoke({"path": str(tmp_path)})
    assert not result.is_error
    assert "child/" in result.content
    assert "deep.txt" not in result.content


# 功能：验证 list_dir 分页——第二页显示后续条目且提示用 page 继续
# 设计：monkeypatch 把 _PAGE_SIZE 调成 2，创建 3 个文件，page=1 显示前 2 个并提示剩余；page=2 显示最后一个
@pytest.mark.asyncio
async def test_list_dir_pagination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import weavecode.core.tools.builtin.list_dir as ld

    monkeypatch.setattr(ld, "_PAGE_SIZE", 2)
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("x")

    r1 = await ListDirTool().invoke({"path": str(tmp_path), "page": 1})
    assert not r1.is_error
    assert "a.txt" in r1.content and "b.txt" in r1.content
    assert "c.txt" not in r1.content
    assert "Use page=2 to see more" in r1.content

    r2 = await ListDirTool().invoke({"path": str(tmp_path), "page": 2})
    assert not r2.is_error
    assert "c.txt" in r2.content
    assert "a.txt" not in r2.content


# 功能：验证空目录显示 0 entries 提示
# 设计：空目录调用，断言 content 含 "(0 entries"
@pytest.mark.asyncio
async def test_list_dir_empty(tmp_path: Path) -> None:
    result = await ListDirTool().invoke({"path": str(tmp_path)})
    assert not result.is_error
    assert "(0 entries" in result.content


# 功能：验证 page 越界返回错误而非崩溃
# 设计：只有 1 个文件却请求 page=5，断言 is_error 且消息含 out of range
@pytest.mark.asyncio
async def test_list_dir_page_out_of_range(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    result = await ListDirTool().invoke({"path": str(tmp_path), "page": 5})
    assert result.is_error
    assert "out of range" in result.content


# 功能：验证目录不存在返回错误并给相似目录建议
# 设计：同目录下建相似目录名，断言错误消息含 Did you mean 且建议带 /
@pytest.mark.asyncio
async def test_list_dir_missing_dir_suggests(tmp_path: Path) -> None:
    (tmp_path / "my_config").mkdir()
    result = await ListDirTool().invoke({"path": str(tmp_path / "my_configs")})
    assert result.is_error
    assert "Directory not found" in result.content
    assert "Did you mean" in result.content
    assert "my_config/" in result.content


# 功能：验证传入文件路径时报错并引导用 read_file
# 设计：传入一个文件路径，断言 is_error 且消息含 Not a directory / read_file
@pytest.mark.asyncio
async def test_list_dir_file_path_rejected(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x")
    result = await ListDirTool().invoke({"path": str(f)})
    assert result.is_error
    assert "Not a directory" in result.content
    assert "read_file" in result.content


# 功能：验证相对路径被拒绝（只支持绝对路径）
# 设计：传相对路径，断言 is_error 且消息含 Invalid path / absolute
@pytest.mark.asyncio
async def test_list_dir_relative_path_rejected(tmp_path: Path) -> None:
    result = await ListDirTool().invoke({"path": "subdir"})
    assert result.is_error
    assert "Invalid path" in result.content
    assert "absolute path" in result.content
