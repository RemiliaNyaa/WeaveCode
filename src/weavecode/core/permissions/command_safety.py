from __future__ import annotations

from tree_sitter import Node

from weavecode.core.permissions.shell_parse import (
    command_name,
    command_tokens,
    iter_command_nodes,
    iter_nodes_of_type,
    node_text,
    parse,
    unquote,
)

# ── 危险命令黑名单（硬编码，用户不可配置）──────────────────────────────────
#
# 设计：黑名单是**安全底线**——命中即强制审批，且不可被「始终允许」缓存绕过。
# 其余命令一律放行（对齐 opencode 的宽松工具层），越界访问另由 external_directory 拦截。

# 直接判危险：提权 / 磁盘级操作 / 不可逆销毁 / 关机重启
_DANGEROUS_COMMANDS: frozenset[str] = frozenset({
    "sudo", "su", "doas",              # 提权
    "mkfs", "mkswap", "fdisk", "parted", "diskpart",  # 格式化 / 分区
    "dd", "shred",                     # 裸设备写入 / 不可恢复擦除
    "shutdown", "reboot", "halt", "poweroff",         # 关机重启
})

# 透明前缀：自身无害，剥掉后继续判定其后的真实命令（如 env / nohup / xargs）
_TRANSPARENT_WRAPPERS: frozenset[str] = frozenset({
    "env", "nohup", "nice", "time", "command", "builtin", "exec", "xargs", "stdbuf",
})

# shell 启动器：其 -c / -Command 后的脚本文本需要递归解析
_SHELL_LAUNCHERS: frozenset[str] = frozenset({
    "bash", "sh", "zsh", "dash", "ash", "ksh", "fish", "pwsh", "powershell", "cmd",
})
_SHELL_SCRIPT_FLAGS: frozenset[str] = frozenset({
    "-c", "-lc", "-ic", "/c", "/k", "-command",
})

# find 的危险参数（可执行任意命令 / 删除文件）
_UNSAFE_FIND_FLAGS: frozenset[str] = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fls", "-fprint", "-fprint0", "-fprintf",
})

# 写文件的重定向操作符（覆盖任意文件）
_WRITE_REDIRECT_OPS: frozenset[str] = frozenset({">", ">>", "&>", "&>>", ">|"})


# 判断整条 bash 命令是否命中危险黑名单
def is_dangerous_command(command: str) -> bool:
    return _is_dangerous(command)


# 递归判定：遍历所有 command 节点 + 所有写文件重定向
def _is_dangerous(command: str) -> bool:
    root = parse(command)
    if root is None:
        return False
    for cmd_node in iter_command_nodes(root):
        if _command_node_is_dangerous(cmd_node):
            return True
    for node in iter_nodes_of_type(root, frozenset({"file_redirect"})):
        if _is_dangerous_redirect(node):
            return True
    return False


# 判定单个 command 节点是否危险（含透明前缀剥离与 shell 启动器递归）
def _command_node_is_dangerous(cmd_node: Node) -> bool:
    name = command_name(cmd_node)
    if not name:
        return False
    args = command_tokens(cmd_node)[1:]

    if name in _DANGEROUS_COMMANDS or name.startswith("mkfs"):
        return True
    if name in _TRANSPARENT_WRAPPERS:
        return _is_dangerous(" ".join(_strip_wrapper_args(args)))
    if name in _SHELL_LAUNCHERS:
        return _shell_launcher_is_dangerous(args)
    if name == "rm":
        return any(_is_force_flag(a) for a in args)
    if name == "find":
        return any(a in _UNSAFE_FIND_FLAGS for a in args)
    if name == "ln":
        return any(_is_symbolic_flag(a) for a in args)
    return False


# rm 的强制删除标志：-f / -rf / -fr / --force（组合短选项里含 f 也算）
def _is_force_flag(arg: str) -> bool:
    if arg == "--force":
        return True
    if arg.startswith("--") or not arg.startswith("-"):
        return False
    return "f" in arg[1:]


# ln 的符号链接标志：-s / --symbolic（组合短选项里含 s 也算）
def _is_symbolic_flag(arg: str) -> bool:
    if arg == "--symbolic":
        return True
    if arg.startswith("--") or not arg.startswith("-"):
        return False
    return "s" in arg[1:]


# 剥掉透明前缀自身消耗的参数（前缀的 flag 与 env 的 VAR=VAL），返回其后的真实命令 token
# 注意：只剥「命令名之前」的参数，命令名之后的 flag 属于真实命令，必须保留
def _strip_wrapper_args(args: list[str]) -> list[str]:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            i += 1
            continue
        head = arg.split("=", 1)[0]
        if "=" in arg and head and head.replace("_", "").isalnum() and not head[0].isdigit():
            i += 1  # env 的 VAR=VAL 赋值
            continue
        break
    return args[i:]


# shell 启动器：取出 -c / -Command 后的脚本文本递归判定
def _shell_launcher_is_dangerous(args: list[str]) -> bool:
    for i, arg in enumerate(args):
        if arg.lower() in _SHELL_SCRIPT_FLAGS and i + 1 < len(args):
            inner = unquote(args[i + 1])
            if inner and _is_dangerous(inner):
                return True
    return False


# 判定一条 file_redirect 是否是「写文件」重定向
def _is_dangerous_redirect(node: Node) -> bool:
    text = node_text(node).strip()
    if not text:
        return False
    op = text.split(None, 1)[0]
    return op in _WRITE_REDIRECT_OPS
