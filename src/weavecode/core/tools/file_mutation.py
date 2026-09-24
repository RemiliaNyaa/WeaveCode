from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


# 把一个路径规范化成「锁的键」：绝对化 + 解析符号链接 + 按平台折叠大小写
# （Windows 不区分大小写，A.txt 与 a.txt 是同一个文件，不折叠就会漏锁）
def canonical_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


# 编辑被拒绝（例如 old_string 在文件里找不到）——由 transform 抛出，交给工具转成 ToolResult
class EditRejectedError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 持有者 + 等待者；归零就把表项删掉，避免锁表只增不减
    users: int = 0


# 一次「取锁」的守卫：进入时引用计数 +1，退出时放锁并 -1，归零即删表项
class _LockGuard:
    def __init__(self, locks: PathLocks, key: str) -> None:
        self._locks = locks
        self._key = key
        self._entry: _Entry | None = None

    # 进入：这个键第一次出现就新造一把锁塞进表；已有就复用（同一个键 = 同一把锁）；计数 +1
    async def __aenter__(self) -> None:
        entry = self._locks._entries.get(self._key)
        if entry is None:
            entry = _Entry()
            self._locks._entries[self._key] = entry
        entry.users += 1
        self._entry = entry
        try:
            await entry.lock.acquire()
        except BaseException:
            # 等待期间被取消：把计数还回去，否则表项会被永久占着
            self._dec_users()
            raise

    # 退出：先放锁（让下一个等待者进来），再计数 -1
    async def __aexit__(self, *exc: object) -> None:
        entry = self._entry
        if entry is not None:
            entry.lock.release()
        self._dec_users()

    # 计数 -1，减到 0 就删表项（放锁与计数分开：取消路径只回退计数）
    def _dec_users(self) -> None:
        entry = self._entry
        self._entry = None
        if entry is None:
            return
        entry.users -= 1
        if entry.users == 0:
            self._locks._entries.pop(self._key, None)


# 按「规范化路径」的锁表：同一个文件排队、不同文件并行
class PathLocks:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    # 取锁上下文：with 住的那段代码对同一个文件互斥
    def hold(self, path: Path) -> _LockGuard:
        return _LockGuard(self, canonical_key(path))

    # 当前表项数（测试用：验证引用计数把表清干净了）
    def size(self) -> int:
        return len(self._entries)


# 让一段 await 不被取消打断：被取消时先等它跑完，再把取消抛出去
#
# 必要性：asyncio.to_thread 跑在独立线程里，取消协程**不会停线程**。如果这时直接放锁，
# 另一个协程就会进来和那个"还在写的线程"同时动同一个文件。所以必须等线程收工。
async def _uninterruptible(awaitable: Awaitable[T]) -> T:
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


# 所有写文件操作的唯一入口：按规范化路径取锁 → 在独立线程里跑完整段「读 → 改 → 写」
#
# 工具自己不锁，统一交给这里锁 —— 从机制上消除「谁忘了加锁」（对齐 OpenCode V2 的 FileMutation）。
class FileMutation:
    def __init__(self, locks: PathLocks | None = None) -> None:
        self._locks = locks or PathLocks()

    # 整文件覆盖写（不存在则创建，自动建父目录）
    async def write(self, path: Path, content: str) -> None:
        await self._run(path, lambda: _write_sync(path, content))

    # 读 → 改 → 写：transform 拿到文件当前文本，返回要写回的新文本；抛异常则中止不写
    async def edit(self, path: Path, transform: Callable[[str], str]) -> None:
        await self._run(path, lambda: _edit_sync(path, transform))

    # 在指定文件的锁内、在线程里执行一段同步逻辑（整段读改写一口气跑完，中间不让出）
    async def _run(self, path: Path, action: Callable[[], T]) -> T:
        async with self._locks.hold(path):
            return await _uninterruptible(asyncio.to_thread(action))

    # 当前锁表大小（测试用）
    def lock_count(self) -> int:
        return self._locks.size()


# 同步核心：覆盖写（整段在线程内完成）
def _write_sync(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# 同步核心：读 → transform → 写（newline="" 保留原始行尾，便于调用方按同样式替换）
def _edit_sync(path: Path, transform: Callable[[str], str]) -> None:
    with path.open("r", encoding="utf-8", newline="") as f:
        content = f.read()
    new_content = transform(content)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(new_content)


# 进程级单例：整个 daemon 共用一张锁表
# （「同一个文件」是全局概念——跨 run、跨 session 都该排队）
DEFAULT_FILE_MUTATION = FileMutation()
