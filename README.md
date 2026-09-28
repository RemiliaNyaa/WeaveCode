# WeaveCode

WeaveCode 是一个本地运行的 AI Agent 系统：一个常驻的 `weave-core` 守护进程负责真正执行任务，`weave` 命令行与 `weave-tui` 终端界面只是连到它的两个客户端。

开发周期：**2026.06 – 2026.09**。

## 架构

```text
用户
  │
  ├─ weave CLI        一次性命令、chat 会话、trace 查看、守护进程管理
  │
  └─ weave-tui        终端 UI、实时事件流、权限审批、上下文水位
        │
        ▼
  weave-core daemon   AgentRunner / AgentLoop / EventBus / SessionManager
        │
        ├─ LLM Provider      模型调用、流式响应与指数退避重试
        ├─ ToolRegistry      内置工具 + 子 Agent + MCP 工具统一注册调用
        ├─ Permission Manager 工具权限判定、越界拦截与审批
        ├─ Compactor         上下文预算估算与双层压缩
        ├─ Session Store     会话与消息持久化（SQLite）
        ├─ Skills            可复用的提示词技能
        ├─ Subagents         派生干净上下文的子 Agent
        ├─ Trace Writer      三层时间线记录
        └─ Event Writer      事件流落盘
```

系统从第一天起就是双进程的，而不是先写成单进程脚本再拆：

- **`weave-core`（守护进程）**：唯一执行体。任务执行、模型调用、工具调用、事件记录都发生在它内部，启动后监听本机 TCP 端口（默认 `127.0.0.1:7437`），等待客户端连接。
- **`weave`（命令行）**：一次性命令，负责发起请求、打印结果，本身不持有状态。
- **`weave-tui`（终端界面）**：常驻的交互式客户端，实时呈现守护进程里的执行过程。

客户端与守护进程之间用一条 TCP 连接通信：消息是**一行一个 JSON**（NDJSON），外壳遵循 **JSON-RPC 2.0**——请求带 `id`、响应回填 `id`、错误有结构化错误码，服务端还可以主动推送事件。协议模型由 pydantic 定义在 `core/bus/` 里，坏消息在进程边界就被挡回去。

这条边界带来三个直接的好处：

1. 所有跨进程数据从第一天起就必须可序列化；
2. 所有命令都有明确的请求、响应与错误格式；
3. 前端、事件订阅、权限审批、子 Agent 都能复用同一条通道。

## 快速开始

前置条件：Python 3.12 与 [`uv`](https://docs.astral.sh/uv/)。

```bash
# 安装依赖（可编辑模式装进虚拟环境）
uv sync
```

复制一份环境配置并填入模型 API Key：

```bash
cp .env.example .env
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
| `weave-core` | 前台启动守护进程（Agent 的执行核心） | 已配置 `.env`（需 API Key） |
| `weave core start` | 后台启动守护进程并写 PID 文件 | 已配置 `.env`（需 API Key） |
| `weave core stop` | 按 PID 停止后台守护进程 | 已有后台守护进程 |
| `weave core status` | 查看守护进程是否存活 | — |
| `weave ping` | 验证守护进程是否连通 | 守护进程已启动 |
| `weave run --goal "..."` | 非交互式执行一次任务 | 守护进程已启动 |
| `weave chat` | 多轮交互式会话 | 守护进程已启动 |
| `weave trace` | 查询与实时跟踪时间线 | 守护进程已启动 |
| `weave-tui` | 交互式终端界面（实时看执行过程） | 守护进程已启动 |
| `weave --version` | 查看版本号 | — |

### `weave-core` —— 启动守护进程

真正执行 Agent 任务的是守护进程，CLI 与 TUI 都只是连接它的客户端。启动时读取配置、打开数据库、初始化日志、监听 TCP 端口，然后等待客户端连接。

```bash
uv run weave-core
```

```text
level=INFO ts=2026-09-28T19:12:41 source=weavecode.core.app msg="weave-core 0.0.1 listening addr=127.0.0.1:8899"
```

看到 `listening addr=...` 就表示就绪。按 `Ctrl+C` 优雅退出；用 `weave core start` / `weave core stop` 管理后台实例。

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
[run] 20260928-131500-3f9c2a
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

`[run]` 后面是本次运行的 id，事件与 trace 都以它命名；`[step N] planning...` 是 ReAct 循环的一步；`[tool]` 是模型发起的工具调用与结果；最后一行给出成功与否、步数与耗时。任务失败时进程以非零码退出。

### `weave chat` —— 多轮会话

在终端里和 Agent 连续对话，回复流式打印；遇到需要审批的工具调用时，终端会给出选项，此时输入的是审批决定而不是聊天内容：

```bash
uv run weave chat
```

```text
[session: sess-1a2b3c4d5e6f]
> 帮我看看这个项目有哪些入口命令
[tool] read_file
[permission] write_file  /project/notes.md
  y=allow once  a=always allow  n=reject
y
...
[waiting for input]
```

### `weave trace` —— 时间线查看

读取 trace 文件，按运行、层、方向过滤；`--follow` 持续尾随新记录，`--raw` 原样输出单行 NDJSON：

```bash
uv run weave trace                            # 全部记录，着色单行展示
uv run weave trace 20260928-131500-3f9c2a     # 只看某次运行
uv run weave trace --layer llm --follow       # 只看 LLM 层并实时尾随
uv run weave trace --direction CORE→LLM --raw # 只看某个方向，输出原始 NDJSON
```

### `weave-tui` —— 交互式终端界面

连接守护进程后提供可视化界面：实时看到模型流式输出、工具调用过程、权限审批卡片与上下文水位。这是日常使用的主前端，`weave run` 只是脚本化的简化客户端。

```bash
uv run weave-tui
```

### 怎么选

- 想确认守护进程活着：`weave ping` / `weave core status`
- 想一次性跑完就退出、或写进脚本：`weave run --goal`
- 想连续对话、逐步推进任务：`weave chat` 或 `weave-tui`
- 想事后复盘执行过程：`weave trace`

## 目录结构

```text
WeaveCode/
├── pyproject.toml        # 依赖、入口脚本与工具链配置
├── Makefile              # lint / test / integration-test 等开发命令
├── .env.example          # 环境变量模板
├── .python-version       # 固定 Python 3.12
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
│       │   ├── compact/          # token 估算、预算与压缩
│       │   ├── events/           # 事件总线与事件落盘
│       │   ├── llm/              # 模型客户端、窗口表与路由
│       │   ├── memory/           # AGENTS.md 规则文件加载
│       │   ├── mcp/              # MCP 客户端、服务器管理与工具包装
│       │   ├── permissions/      # 权限策略、判定与持久化
│       │   ├── session/          # 会话模型、存储与管理器
│       │   ├── skills/           # 技能加载与内建技能
│       │   ├── storage/          # SQLite 存储层与编号迁移
│       │   ├── subagent/         # 子 Agent 派生与等待
│       │   ├── tools/            # 工具注册表、调用链与内置工具
│       │   ├── trace/            # 三层时间线（ipc / event / llm）
│       │   └── transport/        # socket server / client / 事件广播
│       ├── cli/                  # 命令行前端
│       │   ├── main.py           # 参数解析与子命令分发
│       │   └── commands/         # ping / run / chat / trace / core / version
│       ├── tui/                  # 终端界面前端
│       └── data/models.json      # 模型上下文窗口表
└── tests/
    ├── unit/                     # 单元测试
    └── integration/              # 双进程端到端测试
```

## 配置说明

### 配置源优先级

配置按下面的顺序逐层覆盖，越靠后优先级越高：

```text
内建默认值 → ~/.weave/config.json（全局） → 项目 .weave/config.json（覆盖全局）
          → .env → WEAVE_* 环境变量（最高）
```

- `WEAVE_CONFIG` 可以显式指定只读哪一个配置文件；
- 两级 JSON 叠加时**项目本地覆盖全局**，同名 MCP server 整体替换而不是字段级合并；
- 写了未知小节或类型不符的键，进程会直接退出并报出键名，避免配置悄悄失效。

### 端口：为什么示例里是 8899

内建默认端口是 `7437`。部分 Windows 机器上 7437 落在系统保留端口段内，绑定会失败：

```bash
# 查看本机保留端口段
netsh interface ipv4 show excludedportrange protocol=tcp
```

因此 `.env.example` 里示例配置改用 `8899`；启动时报 bind 失败时换一个端口即可。端口属于"随机器而变"的差异，放在不入库的 `.env` 里覆盖，不必改动代码常量。

### 日志

| 环境变量 | 作用 | 示例 |
|---|---|---|
| `WEAVE_LOG_LEVEL` | 日志级别 | `INFO` |
| `WEAVE_LOG_FILE` | 日志文件路径，留空只输出到 stderr | `~/.weave/logs/core.log` |
| `WEAVE_LOG_FORMAT` | `text` 人工可读键值对 / `json` 结构化 | `text` |

日志文件按 10MB × 5 滚动，目录不存在时自动创建。

### 常用配置键

| 小节 | 键 | 说明 |
|---|---|---|
| `core` | `host` / `port` | 监听地址与端口（默认 `127.0.0.1:7437`） |
| `logging` | `level` / `file` / `format` | 同上表三项 |
| `agent` | `max_steps` | 一次运行的最大步数（默认 20） |
| `agent` | `repeat_limit` | 连续同一签名工具调用的拦截阈值（默认 3） |
| `llm` | `default_model` | 默认模型 id |
| `llm` | `stream_retries` | 流式传输失败的重试次数（默认 5，退避 2/4/8/16/32 秒） |
| `trace` | `enabled` / `file` / `include_llm_payload` | trace 开关、落盘路径与是否记录请求体 |
| `permission` | `timeout_s` | 审批超时秒数，超时未响应默认拒绝 |
| `compaction` | `auto` / `reserve_tokens` / `keep_tokens` | 压缩开关、为输出预留的 token、压缩后保留的最近原文 |
| `compaction` | `tool_result_limit` / `tool_result_keep` | tool_result 截断触发与保留长度 |

### MCP 配置

MCP server 写在 `config.json` 的 `mcp.mcpServers` 字典里（server 名 → 配置），两个位置都会被读取：

```json
{
  "mcp": {
    "mcpServers": {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {},
        "cwd": ""
      },
      "exa": {
        "url": "https://mcp.exa.ai/mcp",
        "headers": { "Authorization": "Bearer xxx" }
      }
    },
    "refreshOnNotify": true
  }
}
```

- 有 `url` 视为 http 远程 server（`headers` 放认证头），否则视为 stdio 本地进程（`command` / `args` / `env` / `cwd`）；
- 其余通用字段（`type` / `timeout` 等）一律忽略，第三方配置可以直接复用；
- `refreshOnNotify` 控制工具清单是否随通知刷新（默认开），环境变量 `WEAVE_MCP_REFRESH_ON_NOTIFY` 可覆盖；
- 改动配置需要重启守护进程才生效。

## 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| `error: core not running (127.0.0.1:8899)` | 守护进程没启动，先跑 `uv run weave-core` 或 `weave core start` |
| 启动时报 bind 失败 | 端口落在保留段或被占用，参考上文「端口」小节换端口 |
| `weave run` 立刻退出并提示模型调用失败 | `.env` 里没配 API Key，复制 `.env.example` 为 `.env` 后填入 |
| 启动时报配置键未知 | `config.json` 里写了不支持的键，报错信息里带键名，删掉即可 |
| `weave trace` 什么都打印不出来 | trace 尚未产生记录，或被 `WEAVE_TRACE_ENABLED=0` 关掉了 |
| MCP 工具没出现 | server 连接失败，看启动日志里的 `mcp:` 行；远程 server 需要网络可达 |
| 集成测试报缺少密钥 | `tests/integration/` 需要真实 API Key，用 `-m "not integration"` 跳过 |
| 改了代码没生效 | 依赖是可编辑安装，改 `.py` 直接生效；改了依赖或入口则重新 `uv sync` |

## 开发

### 技术栈

- 语言与运行时：Python 3.12，标准库 `asyncio` 承担全部并发
- 数据与协议：`pydantic`（协议模型与工具参数校验）、标准库 `sqlite3`（存储层）
- 模型接入：`anthropic` SDK（流式与提示词缓存）
- 前端：`textual`（TUI）
- 扩展：官方 `mcp` SDK（外部工具）、`tree-sitter` + `tree-sitter-bash`（命令解析）
- 工程：`uv` 管依赖，`ruff` 做 lint，`mypy` strict 做类型检查，`pytest` + `pytest-asyncio` 做测试

### 测试与静态检查

```bash
# 一键三件套
make lint             # ruff check src tests scripts + mypy src
make test             # uv run pytest tests/unit -v
make integration-test # uv run pytest tests/integration -v（需要 API Key）

# 等价的直接调用
uv run ruff check src tests scripts
uv run mypy src
uv run pytest tests/unit -v
```

提交前建议至少跑过 `make lint` 与 `make test`。

## 环境要求

- Python 3.12（与 `pyproject.toml` 的依赖范围、静态检查口径一致）
- 仅监听 `127.0.0.1`，不对外网开放
- `weave run` / `weave chat` / `weave-tui` 需要在 `.env` 里配置模型 API Key（复制 `.env.example` 为 `.env` 后按需修改）
