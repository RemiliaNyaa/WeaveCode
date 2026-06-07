# WeaveCode 线上协议

> 本文件由 `scripts/gen_protocol_doc.py` 从 `core/bus/` 的协议模型生成，
> 请勿手工编辑；改模型后重新运行生成脚本。

## 传输与帧规则

- 传输：TCP loopback，客户端与 `weave-core` 守护进程各持一条连接。
- 帧：**每条消息是一行 JSON，以 `\n` 结尾**（NDJSON），读端用 `readline()` 定界，单行上限 1 MB。
- 请求带 `id`，响应回填同一个 `id`；服务端主动推送的事件不带 `id`。
- 一条连接上可以同时出现命令响应与事件推送，按外壳字段分流。

## 信封

### 请求

| 字段 | 类型 | 默认值 |
|---|---|---|
| `jsonrpc` | `'2.0'` | `'2.0'` |
| `id` | `str` |  |
| `method` | `str` |  |
| `params` | `dict[str, Any]` | `{}` |

### 成功响应

| 字段 | 类型 | 默认值 |
|---|---|---|
| `jsonrpc` | `'2.0'` | `'2.0'` |
| `id` | `str` |  |
| `result` | `Any` |  |

### 错误响应

| 字段 | 类型 | 默认值 |
|---|---|---|
| `jsonrpc` | `'2.0'` | `'2.0'` |
| `id` | `str \| None` | `None` |
| `error.code` | `int` |  |
| `error.message` | `str` |  |
| `error.data` | `Any` | `None` |

### 事件推送

| 字段 | 类型 | 默认值 |
|---|---|---|
| `kind` | `'event'` | `'event'` |
| `event` | `dict[str, Any]` |  |

## 错误码

| code | 名称 | 含义 |
|---|---|---|
| `-32700` | Parse error | 收到的不是合法 JSON |
| `-32600` | Invalid Request | JSON-RPC 外壳字段不合法 |
| `-32601` | Method not Found | 没有注册这个 method |
| `-32602` | Invalid Params | 业务参数没有通过模型校验 |
| `-32603` | Internal Error | handler 内部异常 |

handler 可以抛出 `HandlerError(code, message, data)`，由传输层转成同格式的错误响应。

## 命令

### `core.ping`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'core.ping'` | `'core.ping'` |
| `client` | `str` |  |

示例：

```json
{
  "type": "core.ping",
  "client": "cli/0.0.1"
}
```

成功响应的 `result`（`PongResult`）：

| 字段 | 类型 | 默认值 |
|---|---|---|
| `server_version` | `str` |  |
| `uptime_ms` | `int` |  |
| `received_at` | `str` |  |

### `agent.run`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'agent.run'` | `'agent.run'` |
| `goal` | `str` |  |

示例：

```json
{
  "type": "agent.run",
  "goal": "..."
}
```

成功响应的 `result`（`AgentRunResult`）：

| 字段 | 类型 | 默认值 |
|---|---|---|
| `run_id` | `str` |  |

### `event.subscribe`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'event.subscribe'` | `'event.subscribe'` |
| `topics` | `list[str]` |  |
| `scope` | `str` | `'global'` |
| `replay_from_run` | `str \| None` | `None` |

示例：

```json
{
  "type": "event.subscribe",
  "topics": ["run.*", "step.*"],
  "scope": "global",
  "replay_from_run": null
}
```

成功响应的 `result`（`EventSubscribeResult`）：

| 字段 | 类型 | 默认值 |
|---|---|---|
| `subscription_id` | `str` |  |
| `replayed_count` | `int` | `0` |

## 事件

### `core.started`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'core.started'` | `'core.started'` |
| `listen_addr` | `str` |  |
| `version` | `str` |  |

### `run.started`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'run.started'` | `'run.started'` |
| `run_id` | `str` |  |
| `goal` | `str` |  |
| `ts` | `str` |  |

### `run.finished`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'run.finished'` | `'run.finished'` |
| `run_id` | `str` |  |
| `status` | `str` |  |
| `reason` | `str \| None` | `None` |
| `steps` | `int` |  |
| `ts` | `str` |  |

### `step.started`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'step.started'` | `'step.started'` |
| `run_id` | `str` |  |
| `step` | `int` |  |
| `ts` | `str` |  |

### `step.finished`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'step.finished'` | `'step.finished'` |
| `run_id` | `str` |  |
| `step` | `int` |  |
| `ts` | `str` |  |

### `tool.call_started`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'tool.call_started'` | `'tool.call_started'` |
| `run_id` | `str` |  |
| `tool_use_id` | `str` |  |
| `tool_name` | `str` |  |
| `params` | `dict[str, Any]` |  |
| `ts` | `str` |  |

### `tool.call_finished`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'tool.call_finished'` | `'tool.call_finished'` |
| `run_id` | `str` |  |
| `tool_use_id` | `str` |  |
| `tool_name` | `str` |  |
| `elapsed_ms` | `int` |  |
| `output` | `str` | `''` |
| `ts` | `str` |  |

### `llm.token`

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type` | `'llm.token'` | `'llm.token'` |
| `run_id` | `str` |  |
| `token` | `str` |  |
| `ts` | `str` |  |
