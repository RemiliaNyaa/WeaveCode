"""从协议模型生成 WIRE_PROTOCOL.md。

代码是协议的事实来源：命令、事件与错误码都定义在 ``core/bus/`` 里，
文档由本脚本重新生成，避免每次加模型都要手工同步一次。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from weavecode.core.bus import commands as commands_module
from weavecode.core.bus.commands import Command
from weavecode.core.bus.envelope import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)
from weavecode.core.bus.events import Event

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "WIRE_PROTOCOL.md"

# JSON-RPC 错误码表（与 envelope.py 保持一致）
_ERROR_CODES = (
    (PARSE_ERROR, "Parse error", "收到的不是合法 JSON"),
    (INVALID_REQUEST, "Invalid Request", "JSON-RPC 外壳字段不合法"),
    (METHOD_NOT_FOUND, "Method not Found", "没有注册这个 method"),
    (INVALID_PARAMS, "Invalid Params", "业务参数没有通过模型校验"),
    (INTERNAL_ERROR, "Internal Error", "handler 内部异常"),
)


# 展开 Annotated[Union[...], Discriminator(...)]，返回按声明顺序排列的模型类
def _members(alias: Any) -> list[type[BaseModel]]:
    args = get_args(alias)
    union = args[0] if args else alias
    candidates = get_args(union) or (union,)
    return [m for m in candidates if isinstance(m, type) and issubclass(m, BaseModel)]


# 把注解渲染成表格里可读的类型文本
def _type_text(annotation: Any) -> str:
    if annotation is None:
        return "any"
    if get_origin(annotation) is Literal:
        return " \\| ".join(repr(v) for v in get_args(annotation))
    return str(annotation).replace("typing.", "")


# 取字段默认值的表格文本；没有默认值时留空
def _default_text(model: type[BaseModel], name: str) -> str:
    field_info = model.model_fields[name]
    if field_info.is_required():
        return ""
    default = field_info.get_default(call_default_factory=True)
    return "`" + repr(default) + "`" if default is not None else ""


# 按注解给必填字段挑一个示例值
def _sample(annotation: Any) -> Any:
    if get_origin(annotation) is Literal:
        return get_args(annotation)[0]
    if annotation is str:
        return "..."
    if annotation is int:
        return 0
    if annotation is bool:
        return True
    if get_origin(annotation) in (list, tuple):
        return []
    if get_origin(annotation) is dict:
        return {}
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _example(annotation)
    return None


# 构造一份示例 payload：有默认值用默认值，必填字段按类型补占位
def _example(model: type[BaseModel]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, field_info in model.model_fields.items():
        if field_info.is_required():
            values[name] = _sample(field_info.annotation)
        else:
            values[name] = field_info.get_default(call_default_factory=True)
    return values


# 渲染一个模型的字段表
def _field_table(model: type[BaseModel]) -> list[str]:
    lines = ["| 字段 | 类型 | 默认值 |", "|---|---|---|"]
    for name in model.model_fields:
        lines.append(f"| `{name}` | `{_type_text(model.model_fields[name].annotation)}`"
                     f" | {_default_text(model, name)} |")
    return lines


# 按 type 字段给模型起标题（命令用 method，事件用 type）
def _title_of(model: type[BaseModel]) -> str:
    field_info = model.model_fields.get("type")
    if field_info is None:
        return model.__name__
    default = field_info.get_default(call_default_factory=True)
    return str(default) if default is not None else model.__name__


# 模型名去掉 Command/Result 后缀，用于把命令和它的结果配对
def _base_name(class_name: str) -> str:
    for suffix in ("Command", "Result"):
        if class_name.endswith(suffix):
            return class_name[: -len(suffix)]
    return class_name


# 渲染命令章节：每个命令一张字段表 + 示例 payload + 对应结果
def _render_commands() -> list[str]:
    results = {
        _base_name(cls.__name__): cls
        for cls in vars(commands_module).values()
        if isinstance(cls, type) and issubclass(cls, BaseModel) and cls.__name__.endswith("Result")
    }
    lines: list[str] = []
    for model in _members(Command):
        lines.append(f"### `{_title_of(model)}`")
        lines.append("")
        lines.extend(_field_table(model))
        lines.append("")
        lines.append("示例：")
        lines.append("")
        lines.append("```json")
        lines.append(_to_json(_example(model)))
        lines.append("```")
        lines.append("")
        result = results.get(_base_name(model.__name__))
        if result is not None:
            lines.append(f"成功响应的 `result`（`{_title_of(result)}`）：")
            lines.append("")
            lines.extend(_field_table(result))
            lines.append("")
    return lines


# 渲染事件章节：每个事件一张字段表
def _render_events() -> list[str]:
    lines: list[str] = []
    for model in _members(Event):
        lines.append(f"### `{_title_of(model)}`")
        lines.append("")
        lines.extend(_field_table(model))
        lines.append("")
    return lines


# 用紧凑 JSON 输出示例 payload
def _to_json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


# 渲染整份协议文档
def _render() -> str:
    lines: list[str] = []
    lines.append("# WeaveCode 线上协议")
    lines.append("")
    lines.append("> 本文件由 `scripts/gen_protocol_doc.py` 从 `core/bus/` 的协议模型生成，")
    lines.append("> 请勿手工编辑；改模型后重新运行生成脚本。")
    lines.append("")
    lines.append("## 传输与帧规则")
    lines.append("")
    lines.append("- 传输：TCP loopback，客户端与 `weave-core` 守护进程各持一条连接。")
    lines.append("- 帧：**每条消息是一行 JSON，以 `\\n` 结尾**（NDJSON），"
                 "读端用 `readline()` 定界，单行上限 1 MB。")
    lines.append("- 请求带 `id`，响应回填同一个 `id`；服务端主动推送的事件不带 `id`。")
    lines.append("- 一条连接上可以同时出现命令响应与事件推送，按外壳字段分流。")
    lines.append("")
    lines.append("## 信封")
    lines.append("")
    lines.append("### 请求")
    lines.append("")
    lines.append("| 字段 | 类型 | 默认值 |")
    lines.append("|---|---|---|")
    lines.append("| `jsonrpc` | `'2.0'` | `'2.0'` |")
    lines.append("| `id` | `str` |  |")
    lines.append("| `method` | `str` |  |")
    lines.append("| `params` | `dict[str, Any]` | `{}` |")
    lines.append("")
    lines.append("### 成功响应")
    lines.append("")
    lines.append("| 字段 | 类型 | 默认值 |")
    lines.append("|---|---|---|")
    lines.append("| `jsonrpc` | `'2.0'` | `'2.0'` |")
    lines.append("| `id` | `str` |  |")
    lines.append("| `result` | `Any` |  |")
    lines.append("")
    lines.append("### 错误响应")
    lines.append("")
    lines.append("| 字段 | 类型 | 默认值 |")
    lines.append("|---|---|---|")
    lines.append("| `jsonrpc` | `'2.0'` | `'2.0'` |")
    lines.append("| `id` | `str \\| None` | `None` |")
    lines.append("| `error.code` | `int` |  |")
    lines.append("| `error.message` | `str` |  |")
    lines.append("| `error.data` | `Any` | `None` |")
    lines.append("")
    lines.append("### 事件推送")
    lines.append("")
    lines.append("| 字段 | 类型 | 默认值 |")
    lines.append("|---|---|---|")
    lines.append("| `kind` | `'event'` | `'event'` |")
    lines.append("| `event` | `dict[str, Any]` |  |")
    lines.append("")
    lines.append("## 错误码")
    lines.append("")
    lines.append("| code | 名称 | 含义 |")
    lines.append("|---|---|---|")
    for code, name, meaning in _ERROR_CODES:
        lines.append(f"| `{code}` | {name} | {meaning} |")
    lines.append("")
    lines.append("handler 可以抛出 `HandlerError(code, message, data)`，"
                 "由传输层转成同格式的错误响应。")
    lines.append("")
    lines.append("## 命令")
    lines.append("")
    lines.extend(_render_commands())
    lines.append("## 事件")
    lines.append("")
    lines.extend(_render_events())
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(prog="gen_protocol_doc", description="生成 WIRE_PROTOCOL.md")
    parser.add_argument("--check", action="store_true", help="只比对不写入，不一致时退出码 1")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT), help="输出文件路径")
    args = parser.parse_args()

    content = _render()
    output = Path(args.output)

    if args.check:
        current = output.read_text(encoding="utf-8") if output.exists() else ""
        if current != content:
            print(f"{output} 已过期，请重新运行 scripts/gen_protocol_doc.py", file=sys.stderr)
            return 1
        print(f"{output} 已与协议模型一致")
        return 0

    output.write_text(content, encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
