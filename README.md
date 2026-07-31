# WeaveCode

WeaveCode 是一个本地运行的 AI Agent 系统：一个常驻的 `weave-core` 守护进程负责真正执行任务，`weave` 命令行与 `weave-tui` 终端界面只是连到它的两个客户端。

## 架构

```text
用户
  │
  ├─ weave CLI        一次性命令、chat 会话、trace 查看
  │
  └─ weave-tui        终端 UI、实时事件流
        │
        ▼
  weave-core daemon   AgentRunner / AgentLoop / EventBus / SessionManager
        │
        ├─ LLM Provider      模型调用与流式响应
        ├─ ToolRegistry      内置工具的注册与调用
        ├─ Session Store     会话与消息持久化
        ├─ Trace Writer      三层时间线记录
        └─ Event Writer      事件流落盘
```

系统从第一天起就是双进程的，而不是先写成单进程脚本再拆：

- **`weave-core`（守护进程）**：唯一执行体。任务执行、模型调用、工具调用、事件记录都发生在它内部，启动后监听本机 TCP 端口（默认 `127.0.0.1:7437`），等待客户端连接。
- **`weave`（命令行）**：一次性命令，负责发起请求、打印结果，本身不持有状态。
- **`weave-tui`（终端界面）**：常驻的交互式客户端，实时呈现守护进程里的执行过程。

客户端与守护进程之间用一条 TCP 连接通信：消息是**一行一个 JSON**（NDJSON），外壳遵循 **JSON-RPC 2.0**——请求带 `id`、响应回填 `id`、错误有结构化错误码，服务端还可以主动推送事件。协议模型由 pydantic 定义在 `core/bus/` 里，坏消息在进程边界就被挡回去；仓库根的 `WIRE_PROTOCOL.md` 由 `scripts/gen_protocol_doc.py` 从这些模型生成，不会手工漂移。

这条边界带来三个直接的好处：

1. 所有跨进程数据从第一天起就必须可序列化；
2. 所有命令都有明确的请求、响应与错误格式；
3. 后面新增的前端、事件订阅、任务执行都能复用同一条通道。

## 快速开始

前置条件：Python 3.12 与 [`uv`](https://docs.astral.sh/uv/)。

```bash
# 安装依赖（可编辑模式装进虚拟环境）
uv sync
```

装好后开两个终端。终端 A 启动守护进程：

```bash
uv run weave-core
```

终端 B 发一次握手：

```bash
uv run weave ping
```

看到这样一行就说明链路通了：

```text
pong server=0.0.1 uptime=150ms latency=2ms
```

`server` 是守护进程的版本号，`uptime` 是它已运行的时长，`latency` 是本次请求的往返耗时。守护进程用 `Ctrl+C` 停止。

## 运行指令

| 指令 | 作用 | 前置条件 |
|------|------|----------|
| `weave-core` | 启动守护进程（Agent 的执行核心） | 已配置 `.env`（需 API Key） |
| `weave ping` | 验证守护进程是否连通 | 守护进程已启动 |
| `weave run --goal "..."` | 非交互式执行一次任务 | 守护进程已启动 |
| `weave chat` | 多轮交互式会话 | 守护进程已启动 |
| `weave trace` | 查询与实时跟踪时间线 | 守护进程已启动 |
| `weave-tui` | 交互式终端界面（实时看执行过程） | 守护进程已启动 |
| `weave --version` | 查看版本号 | — |

### `weave-core` —— 启动守护进程

真正执行 Agent 任务的是守护进程，CLI 与 TUI 都只是连接它的客户端。启动时读取配置、初始化日志、监听 TCP 端口，然后等待客户端连接。

```bash
uv run weave-core
```

```text
level=INFO ts=2026-07-31T20:12:41 source=weavecode.core.app msg="weave-core 0.0.1 listening addr=127.0.0.1:7437"
```

看到 `listening addr=...` 就表示就绪。按 `Ctrl+C` 优雅退出。

### `weave ping` —— 探活

向守护进程发一个 JSON-RPC `ping`，验证链路是否通畅：

```bash
uv run weave ping
```

```text
pong server=0.0.1 uptime=27530ms latency=0ms
```

### `weave run --goal "..."` —— 按目标执行任务

把目标交给守护进程，Agent 自主规划、调用工具、给出结果；执行进度逐行打印到终端，适合脚本化验证链路：

```bash
uv run weave run --goal "读取当前目录下 README.md 的内容，然后简要总结这个项目是做什么的。"
```

```text
[run] 20260731-131500-3f9c2a
[step 1] planning...
[tool] read_file {"path": "README.md"}
[tool] read_file ✓  0ms
[step 1] done
[step 2] planning...
我已经读取了 README.md 的内容，下面是对这个项目的简要总结：
...
[step 2] done
[run] success  2 steps  6.2s
```

`[run]` 后面是本次运行的 id，事件文件与 trace 都以它命名；`[step N] planning...` 是 ReAct 循环的一步；`[tool]` 是模型发起的工具调用与结果；最后一行给出成功与否、步数与耗时。任务失败时进程以非零码退出。

### `weave chat` —— 多轮会话

在终端里和 Agent 连续对话，回复流式打印；会话历史由守护进程保存，可以跨多轮延续：

```bash
uv run weave chat
```

```text
[session: sess-1a2b3c4d5e6f]
> 帮我看看这个项目有哪些入口命令
...
```

### `weave trace` —— 时间线查看

读取 trace 文件，按运行、层、方向过滤；`--follow` 持续尾随新记录，`--raw` 原样输出单行 NDJSON：

```bash
uv run weave trace                        # 全部记录，着色单行展示
uv run weave trace 20260731-131500-3f9c2a # 只看某次运行
uv run weave trace --layer llm --follow   # 只看 LLM 层并实时尾随
```

### `weave-tui` —— 交互式终端界面

连接守护进程后提供可视化界面：实时看到模型流式输出、工具调用过程与事件流。这是日常使用的主前端，`weave run` 只是脚本化的简化客户端。

```bash
uv run weave-tui
```

### 怎么选

- 想确认守护进程活着：`weave ping`
- 想一次性跑完就退出、或写进脚本：`weave run --goal`
- 想连续对话、逐步推进任务：`weave chat` 或 `weave-tui`
- 想事后复盘执行过程：`weave trace`

## 目录结构

```text
WeaveCode/
├── pyproject.toml        # 依赖、入口脚本与工具链配置
├── Makefile              # lint / test / integration-test 等开发命令
├── .env.example          # 环境变量模板
├── WIRE_PROTOCOL.md      # 线上协议文档（由脚本生成）
├── scripts/              # 仓库脚本（协议文档生成等）
├── src/
│   └── weavecode/
│       ├── core/                 # 守护进程与全部核心能力
│       │   ├── app.py            # 守护进程入口：装配各模块并监听端口
│       │   ├── config.py         # 四级优先级配置
│       │   ├── logging_setup.py  # 日志初始化（文本 / JSON 两种格式）
│       │   ├── context.py        # system prompt 组装
│       │   ├── loop.py           # AgentLoop：思考 → 工具 → 观察
│       │   ├── runner.py         # 串联 Loop 与 EventBus
│       │   ├── bus/              # 协议模型：信封、命令、事件
│       │   ├── events/           # 事件总线与 events.jsonl 落盘
│       │   ├── llm/              # 模型客户端与上下文窗口表
│       │   ├── session/          # 会话模型、存储与管理器
│       │   ├── task/             # 任务列表
│       │   ├── tools/            # 工具注册表、调用链与内置工具
│       │   ├── trace/            # 三层时间线（ipc / event / llm）
│       │   └── transport/        # socket server / client / 事件广播
│       ├── cli/                  # 命令行前端
│       │   ├── main.py           # 参数解析与子命令分发
│       │   └── commands/         # ping / run / chat / trace / core / version
│       └── tui/                  # 终端界面前端
└── tests/
    ├── unit/                     # 单元测试
    └── integration/              # 双进程端到端测试
```

## 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| `error: core not running (127.0.0.1:7437)` | 守护进程没启动，先跑 `uv run weave-core` |
| 启动时报端口已被占用 | 已有一个守护进程在跑，或端口被别的程序占用；用 `WEAVE_PORT` 换一个端口 |
| `weave run` 立刻退出并提示模型调用失败 | `.env` 里没配 API Key，复制 `.env.example` 为 `.env` 后填入 |
| `weave trace` 什么都打印不出来 | trace 尚未产生记录，或被 `WEAVE_TRACE_ENABLED=0` 关掉了 |
| 集成测试报缺少密钥 | `tests/integration/` 需要真实 API Key，用 `-m "not integration"` 跳过 |
| 改了代码没生效 | 依赖是可编辑安装，改 `.py` 直接生效；改了依赖或入口则重新 `uv sync` |

## 环境要求

- Python 3.12（与 `pyproject.toml` 的依赖范围、静态检查口径一致）
- 仅监听 `127.0.0.1`，不对外网开放
- `weave run` / `weave chat` / `weave-tui` 需要在 `.env` 里配置模型 API Key（复制 `.env.example` 为 `.env` 后按需修改）
