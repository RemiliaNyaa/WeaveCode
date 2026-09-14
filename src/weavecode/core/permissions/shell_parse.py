from __future__ import annotations

from collections.abc import Iterator

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

# Bash 语法解析器（模块级复用；tree-sitter 解析器非线程安全，daemon 为单事件循环，安全）
_LANGUAGE = Language(tree_sitter_bash.language())
_PARSER = Parser(_LANGUAGE)


# 解析命令为语法树根节点；解析失败返回 None
def parse(command: str) -> Node | None:
    if not command.strip():
        return None
    try:
        return _PARSER.parse(command.encode("utf-8", errors="replace")).root_node
    except Exception:
        return None


# 递归遍历语法树，产出所有 command 节点（覆盖管道、&&、子 shell、命令替换）
def iter_command_nodes(node: Node) -> Iterator[Node]:
    if node.type == "command":
        yield node
        return
    for child in node.children:
        yield from iter_command_nodes(child)


# 读取节点原文
def node_text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text else ""


# 返回一个 command 节点的各部分原文（[命令名, 参数...]）
def command_tokens(cmd_node: Node) -> list[str]:
    return [node_text(child) for child in cmd_node.children]


# 去掉包裹字符串的引号（单引号/双引号）
def unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text
