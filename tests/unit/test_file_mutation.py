from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from weavecode.core.tools.file_mutation import (
    EditRejectedError,
    FileMutation,
    canonical_key,
)


# 造一个已存在的目标文件，返回路径
def _target(tmp_path: Path, content: str = "") -> Path:
    p = tmp_path / "f.txt"
    p.write_text(content, encoding="utf-8")
    return p


# 功能：锁的键在 Windows 上折叠大小写（A.txt 与 a.txt 是同一个文件），POSIX 上不折叠
# 设计：直接断言平台相关行为——不折叠就会漏锁，两个「不同键」实际打同一文件
def test_canonical_key_folds_case_only_on_windows(tmp_path: Path) -> None:
    a = canonical_key(tmp_path / "A.txt")
    b = canonical_key(tmp_path / "a.txt")

    if sys.platform == "win32":
        assert a == b
    else:
        assert a != b


# 功能：同一个文件的并发「读→改→写」必须排队，不能互相覆盖
# 设计：transform 里 sleep 拉宽「读→写」窗口，让 5 个并发调用各自读到旧内容；
#       断言最终是 5 个标记全在（xxxxx）——没有锁的话会退化成单个 x，这条用例必然失败
@pytest.mark.asyncio
async def test_same_file_edits_are_serialized(tmp_path: Path) -> None:
    target = _target(tmp_path)
    mutation = FileMutation()

    def append_marker(content: str) -> str:
        time.sleep(0.02)
        return content + "x"

    await asyncio.gather(*[mutation.edit(target, append_marker) for _ in range(5)])

    assert target.read_text(encoding="utf-8") == "xxxxx"


# 功能：不同文件的并发写不互相阻塞（锁的粒度是路径，不是全局）
# 设计：4 个文件各 sleep 0.1s；并行约 0.1s、串行约 0.4s——用耗时上限把「并行」这条不变式锁住
@pytest.mark.asyncio
async def test_different_files_run_in_parallel(tmp_path: Path) -> None:
    paths: list[Path] = []
    for i in range(4):
        p = tmp_path / f"f{i}.txt"
        p.write_text("", encoding="utf-8")
        paths.append(p)
    mutation = FileMutation()

    def slow(content: str) -> str:
        time.sleep(0.1)
        return content + "x"

    begin = time.monotonic()
    await asyncio.gather(*[mutation.edit(p, slow) for p in paths])
    elapsed = time.monotonic() - begin

    assert elapsed < 0.3, f"不同文件被串行化了，耗时 {elapsed:.2f}s"


# 功能：锁表用引用计数，全部结束后必须清空（不会只增不减）
# 设计：跑完一批并发编辑后断言 lock_count()==0——这是 V2 用引用计数补掉的那个内存泄漏
@pytest.mark.asyncio
async def test_lock_table_is_reclaimed(tmp_path: Path) -> None:
    target = _target(tmp_path, "a")
    mutation = FileMutation()

    await asyncio.gather(*[mutation.edit(target, lambda c: c + "x") for _ in range(3)])

    assert mutation.lock_count() == 0


# 功能：transform 抛异常时中止写入，文件保持原样（且锁表照样清空）
# 设计：这是「拒绝编辑」的路径（如 old_string 找不到）；断言文件未被改动 + 锁未泄漏
@pytest.mark.asyncio
async def test_transform_error_leaves_file_untouched(tmp_path: Path) -> None:
    target = _target(tmp_path, "original")
    mutation = FileMutation()

    def reject(content: str) -> str:
        raise EditRejectedError("old_string not found")

    with pytest.raises(EditRejectedError):
        await mutation.edit(target, reject)

    assert target.read_text(encoding="utf-8") == "original"
    assert mutation.lock_count() == 0


# 功能：临界区不可中断——协程被取消时要等线程写完，文件必须是完整结果
# 设计：to_thread 跑在独立线程里，取消协程不会停线程；若不显式等待就放锁，
#       会出现「放锁后线程还在写」。断言取消后文件已是完整内容，且锁表清空
@pytest.mark.asyncio
async def test_cancellation_waits_for_the_write_to_finish(tmp_path: Path) -> None:
    target = _target(tmp_path)
    mutation = FileMutation()

    def slow(content: str) -> str:
        time.sleep(0.1)
        return "done"

    task = asyncio.create_task(mutation.edit(target, slow))
    await asyncio.sleep(0.02)  # 让它进到线程里
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert target.read_text(encoding="utf-8") == "done"
    assert mutation.lock_count() == 0


# 功能：写入服务不阻塞事件循环——写大文件期间其它协程仍能推进
# 设计：这是把 IO 改成异步的主要收益。用「心跳协程的推进次数」验证，
#       而不是测绝对耗时（避免机器性能影响用例稳定性）
@pytest.mark.asyncio
async def test_write_does_not_block_the_event_loop(tmp_path: Path) -> None:
    target = _target(tmp_path)
    mutation = FileMutation()
    big = "x" * (2 * 1024 * 1024)  # 2 MB

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.005)
            ticks += 1

    await asyncio.gather(mutation.write(target, big), heartbeat())

    assert ticks >= 10, f"事件循环被写入阻塞了，心跳只推进 {ticks} 次"
    assert target.stat().st_size == len(big)
