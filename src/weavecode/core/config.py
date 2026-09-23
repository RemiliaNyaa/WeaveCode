from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7437
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "~/.weave/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.weave/config.json"
_DEFAULT_MAX_STEPS = 20
_DEFAULT_REPEAT_LIMIT = 3
_DEFAULT_STREAM_RETRIES = 5
_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_TRACE_FILE = "~/.weave/traces/daemon.jsonl"


@dataclass
class LoggingConfig:
    level: str = _DEFAULT_LOG_LEVEL
    file: str = _DEFAULT_LOG_FILE
    format: str = _DEFAULT_LOG_FORMAT  # "text" | "json"


@dataclass
class AgentConfig:
    max_steps: int = _DEFAULT_MAX_STEPS
    # 连续多少步发出完全相同的工具调用就判定为重复死循环（见 loop.py 的重复调用闸门）
    repeat_limit: int = _DEFAULT_REPEAT_LIMIT


@dataclass
class LlmConfig:
    default_model: str = _DEFAULT_MODEL
    router: str = "static"  # "static" | "rule_based" (S4) | "cost_budget" (S6)
    # 流式调用失败后最多重试几次（首发不算）；退避按 2/4/8/16/32 秒现场计算
    stream_retries: int = _DEFAULT_STREAM_RETRIES


@dataclass
class TraceConfig:
    enabled: bool = True
    file: str = _DEFAULT_TRACE_FILE
    include_llm_payload: bool = True  # false 时 LLM 记录只保留摘要


@dataclass
class PermissionConfig:
    timeout_s: float = 60.0  # 审批超时秒数；0 表示不超时


@dataclass
class CompactionConfig:
    auto: bool = True             # 是否启用自动压缩
    reserve_tokens: int = 20_000  # 发请求前为本次输出 + 估算容差预留的 token
    keep_tokens: int = 8_000      # 压缩后原样保留的最近历史 token 数
    tool_result_limit: int = 8_000  # tool_result 截断触发字符数
    tool_result_keep: int = 4_000   # 截断后保留的前缀字符数


@dataclass
class McpServerConfig:
    name: str
    command: str = ""              # stdio 专用：可执行文件路径
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""                  # stdio 专用：子进程工作目录
    url: str = ""                  # http 专用：连接端点
    headers: dict[str, str] = field(default_factory=dict)  # http 专用：认证头


@dataclass
class McpConfig:
    servers: list[McpServerConfig] = field(default_factory=list)
    # 是否监听 server 的 notifications/tools/list_changed 并重拉工具清单（默认开）
    refresh_on_notify: bool = True


@dataclass
class WeaveConfig:
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    mcp: McpConfig = field(default_factory=McpConfig)


# 构建并返回运行时配置：默认值 → 全局 JSON → 项目本地 JSON → .env → 系统环境变量（后者优先级最高）
def get_config() -> WeaveConfig:
    config = WeaveConfig()

    # .env 必须在读取 WEAVE_CONFIG 之前加载，以便 .env 中的 WEAVE_CONFIG 能影响 JSON 路径
    load_dotenv(".env", override=False)

    # 若显式指定 WEAVE_CONFIG，只读该文件；否则按优先级叠加：全局 → 项目本地
    explicit = os.environ.get("WEAVE_CONFIG")
    if explicit:
        config_paths = [Path(explicit).expanduser()]
    else:
        config_paths = [
            Path(_DEFAULT_CONFIG_PATH).expanduser(),
            Path(".weave/config.json"),
        ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, "rb") as f:
                    data = json.load(f)
            except json.JSONDecodeError as e:
                raise SystemExit(f"Config parse error ({config_path}): {e}") from e
            _apply_json(config, data)

    _apply_env(config)
    return config


# 将已解析的 JSON 根表写入 config；未知小节或类型错误时退出进程
def _apply_json(config: WeaveConfig, data: dict[str, Any]) -> None:
    unknown = set(data.keys()) - {"core", "logging", "agent", "llm", "trace", "permission", "compaction", "mcp"}
    if unknown:
        raise SystemExit(f"Unknown top-level config keys: {', '.join(sorted(unknown))}")

    if "core" in data:
        core = data["core"]
        if not isinstance(core, dict):
            raise SystemExit("Config error: [core] must be a table")
        unknown_core: set[str] = set(core.keys()) - {"host", "port"}
        if unknown_core:
            raise SystemExit(f"Unknown [core] keys: {', '.join(sorted(unknown_core))}")
        if "host" in core:
            val = core["host"]
            if not isinstance(val, str):
                raise SystemExit("Config error: core.host must be a string")
            config.host = val
        if "port" in core:
            val = core["port"]
            if not isinstance(val, int):
                raise SystemExit("Config error: core.port must be an integer")
            config.port = val

    if "logging" in data:
        log = data["logging"]
        if not isinstance(log, dict):
            raise SystemExit("Config error: logging must be a table")
        unknown_log: set[str] = set(log.keys()) - {"level", "file", "format"}
        if unknown_log:
            raise SystemExit(f"Unknown logging keys: {', '.join(sorted(unknown_log))}")
        for key in ("level", "file", "format"):
            if key in log:
                val = log[key]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: logging.{key} must be a string")
                setattr(config.logging, key, val)

    if "agent" in data:
        agent = data["agent"]
        if not isinstance(agent, dict):
            raise SystemExit("Config error: agent must be a table")
        unknown_agent: set[str] = set(agent.keys()) - {"max_steps", "repeat_limit"}
        if unknown_agent:
            raise SystemExit(f"Unknown agent keys: {', '.join(sorted(unknown_agent))}")
        if "max_steps" in agent:
            val = agent["max_steps"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: agent.max_steps must be a positive integer")
            config.agent.max_steps = val
        if "repeat_limit" in agent:
            val = agent["repeat_limit"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: agent.repeat_limit must be a positive integer")
            config.agent.repeat_limit = val

    if "llm" in data:
        llm = data["llm"]
        if not isinstance(llm, dict):
            raise SystemExit("Config error: llm must be a table")
        unknown_llm: set[str] = set(llm.keys()) - {"default_model", "router", "stream_retries"}
        if unknown_llm:
            raise SystemExit(f"Unknown llm keys: {', '.join(sorted(unknown_llm))}")
        if "default_model" in llm:
            val = llm["default_model"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.default_model must be a string")
            config.llm.default_model = val
        if "router" in llm:
            val = llm["router"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.router must be a string")
            config.llm.router = val
        if "stream_retries" in llm:
            val = llm["stream_retries"]
            if not isinstance(val, int) or val < 0:
                raise SystemExit("Config error: llm.stream_retries must be a non-negative integer")
            config.llm.stream_retries = val

    if "trace" in data:
        trace = data["trace"]
        if not isinstance(trace, dict):
            raise SystemExit("Config error: trace must be a table")
        unknown_trace: set[str] = set(trace.keys()) - {"enabled", "file", "include_llm_payload"}
        if unknown_trace:
            raise SystemExit(f"Unknown trace keys: {', '.join(sorted(unknown_trace))}")
        if "enabled" in trace:
            val = trace["enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.enabled must be a boolean")
            config.trace.enabled = val
        if "file" in trace:
            val = trace["file"]
            if not isinstance(val, str):
                raise SystemExit("Config error: trace.file must be a string")
            config.trace.file = val
        if "include_llm_payload" in trace:
            val = trace["include_llm_payload"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.include_llm_payload must be a boolean")
            config.trace.include_llm_payload = val

    if "permission" in data:
        perm = data["permission"]
        if not isinstance(perm, dict):
            raise SystemExit("Config error: permission must be a table")
        unknown_perm: set[str] = set(perm.keys()) - {"timeout_s"}
        if unknown_perm:
            raise SystemExit(f"Unknown permission keys: {', '.join(sorted(unknown_perm))}")
        if "timeout_s" in perm:
            val = perm["timeout_s"]
            if not isinstance(val, (int, float)) or val < 0:
                raise SystemExit("Config error: permission.timeout_s must be a non-negative number")
            config.permission.timeout_s = float(val)

    if "compaction" in data:
        comp = data["compaction"]
        if not isinstance(comp, dict):
            raise SystemExit("Config error: compaction must be a table")
        unknown_comp: set[str] = set(comp.keys()) - {
            "auto", "reserve_tokens", "keep_tokens", "tool_result_limit", "tool_result_keep",
        }
        if unknown_comp:
            raise SystemExit(f"Unknown compaction keys: {', '.join(sorted(unknown_comp))}")
        if "auto" in comp:
            val = comp["auto"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: compaction.auto must be a boolean")
            config.compaction.auto = val
        if "reserve_tokens" in comp:
            val = comp["reserve_tokens"]
            if not isinstance(val, int) or val < 0:
                raise SystemExit("Config error: compaction.reserve_tokens must be non-negative")
            config.compaction.reserve_tokens = val
        if "keep_tokens" in comp:
            val = comp["keep_tokens"]
            if not isinstance(val, int) or val < 0:
                raise SystemExit("Config error: compaction.keep_tokens must be non-negative")
            config.compaction.keep_tokens = val
        if "tool_result_limit" in comp:
            val = comp["tool_result_limit"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: compaction.tool_result_limit must be a positive integer")
            config.compaction.tool_result_limit = val
        if "tool_result_keep" in comp:
            val = comp["tool_result_keep"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: compaction.tool_result_keep must be a positive integer")
            config.compaction.tool_result_keep = val

    # mcpServers：官方 JSON 格式（mcp 键下 mcpServers 字典，server 名 → 配置）
    # 同名 server 覆盖：后读的（项目本地）直接覆盖先读的（全局），保证"项目本地覆盖全局"
    if "mcp" in data:
        mcp = data["mcp"]
        if not isinstance(mcp, dict):
            raise SystemExit("Config error: mcp must be a table")
        unknown_mcp: set[str] = set(mcp.keys()) - {"mcpServers", "refreshOnNotify"}
        if unknown_mcp:
            raise SystemExit(f"Unknown mcp keys: {', '.join(sorted(unknown_mcp))}")
        if "refreshOnNotify" in mcp:
            val = mcp["refreshOnNotify"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: mcp.refreshOnNotify must be a boolean")
            config.mcp.refresh_on_notify = val
        servers_raw = mcp.get("mcpServers", {})
        if not isinstance(servers_raw, dict):
            raise SystemExit("Config error: mcp.mcpServers must be an object of server configs")
        # 项目本地（后读）的同名 server 覆盖全局（先读）的
        if isinstance(servers_raw, dict):
            for name, srv in servers_raw.items():
                if not isinstance(srv, dict):
                    raise SystemExit(f"Config error: mcp.mcpServers.{name} must be an object")
                # 不解析 type 字段：有 url 视为 http，否则视为 stdio
                s = McpServerConfig(name=name)
                if "url" in srv:
                    # http：只解析 url 和 headers
                    val = srv["url"]
                    if not isinstance(val, str):
                        raise SystemExit(f"Config error: mcp.mcpServers.{name}.url must be a string")
                    s.url = val
                    if "headers" in srv:
                        val = srv["headers"]
                        if not isinstance(val, dict):
                            raise SystemExit(f"Config error: mcp.mcpServers.{name}.headers must be a table")
                        s.headers = {str(k): str(v) for k, v in val.items()}
                else:
                    # stdio：只解析 command、args、env、cwd
                    if "command" in srv:
                        val = srv["command"]
                        if not isinstance(val, str):
                            raise SystemExit(f"Config error: mcp.mcpServers.{name}.command must be a string")
                        s.command = val
                    if "args" in srv:
                        val = srv["args"]
                        if not isinstance(val, list):
                            raise SystemExit(f"Config error: mcp.mcpServers.{name}.args must be an array")
                        s.args = [str(a) for a in val]
                    if "env" in srv:
                        val = srv["env"]
                        if not isinstance(val, dict):
                            raise SystemExit(f"Config error: mcp.mcpServers.{name}.env must be a table")
                        s.env = {str(k): str(v) for k, v in val.items()}
                    if "cwd" in srv:
                        val = srv["cwd"]
                        if not isinstance(val, str):
                            raise SystemExit(f"Config error: mcp.mcpServers.{name}.cwd must be a string")
                        s.cwd = val
                # 其他字段（type/toolSearch/alwaysLoad/timeout 等）一律忽略
                # 同名 server：移除已存在的同名项（全局先读，项目后读 → 项目覆盖全局）
                config.mcp.servers = [x for x in config.mcp.servers if x.name != name]
                config.mcp.servers.append(s)


# 用 WEAVE_* 环境变量覆盖 config 中对应字段（若变量已设置）
def _apply_env(config: WeaveConfig) -> None:
    host = os.environ.get("WEAVE_HOST")
    if host is not None:
        config.host = host

    port_str = os.environ.get("WEAVE_PORT")
    if port_str is not None:
        try:
            config.port = int(port_str)
        except ValueError:
            raise SystemExit(f"Config error: WEAVE_PORT must be an integer, got: {port_str!r}")

    log_level = os.environ.get("WEAVE_LOG_LEVEL")
    if log_level is not None:
        config.logging.level = log_level

    log_file = os.environ.get("WEAVE_LOG_FILE")
    if log_file is not None:
        config.logging.file = log_file

    log_format = os.environ.get("WEAVE_LOG_FORMAT")
    if log_format is not None:
        config.logging.format = log_format

    max_steps_str = os.environ.get("WEAVE_MAX_STEPS")
    if max_steps_str is not None:
        try:
            val = int(max_steps_str)
            if val <= 0:
                raise SystemExit(
                    "Config error: WEAVE_MAX_STEPS must be a positive integer,"
                    f" got: {max_steps_str!r}"
                )
            config.agent.max_steps = val
        except ValueError:
            raise SystemExit(
                f"Config error: WEAVE_MAX_STEPS must be an integer, got: {max_steps_str!r}"
            )

    repeat_limit_str = os.environ.get("WEAVE_REPEAT_LIMIT")
    if repeat_limit_str is not None:
        try:
            val = int(repeat_limit_str)
            if val <= 0:
                raise SystemExit(
                    "Config error: WEAVE_REPEAT_LIMIT must be a positive integer,"
                    f" got: {repeat_limit_str!r}"
                )
            config.agent.repeat_limit = val
        except ValueError:
            raise SystemExit(
                f"Config error: WEAVE_REPEAT_LIMIT must be an integer, got: {repeat_limit_str!r}"
            )

    mcp_refresh = os.environ.get("WEAVE_MCP_REFRESH_ON_NOTIFY")
    if mcp_refresh is not None:
        if mcp_refresh.strip().lower() in {"1", "true", "yes", "on"}:
            config.mcp.refresh_on_notify = True
        elif mcp_refresh.strip().lower() in {"0", "false", "no", "off"}:
            config.mcp.refresh_on_notify = False
        else:
            raise SystemExit(
                "Config error: WEAVE_MCP_REFRESH_ON_NOTIFY must be a boolean,"
                f" got: {mcp_refresh!r}"
            )

    default_model = os.environ.get("WEAVE_LLM_DEFAULT_MODEL")
    if default_model is not None:
        config.llm.default_model = default_model

    stream_retries_str = os.environ.get("WEAVE_LLM_STREAM_RETRIES")
    if stream_retries_str is not None:
        try:
            val = int(stream_retries_str)
            if val < 0:
                raise SystemExit(
                    "Config error: WEAVE_LLM_STREAM_RETRIES must be a non-negative integer,"
                    f" got: {stream_retries_str!r}"
                )
            config.llm.stream_retries = val
        except ValueError:
            raise SystemExit(
                "Config error: WEAVE_LLM_STREAM_RETRIES must be an integer,"
                f" got: {stream_retries_str!r}"
            )

    trace_enabled = os.environ.get("WEAVE_TRACE_ENABLED")
    if trace_enabled is not None:
        config.trace.enabled = trace_enabled.lower() not in ("0", "false", "no")

    trace_file = os.environ.get("WEAVE_TRACE_FILE")
    if trace_file is not None:
        config.trace.file = trace_file

    trace_payload = os.environ.get("WEAVE_TRACE_INCLUDE_LLM_PAYLOAD")
    if trace_payload is not None:
        config.trace.include_llm_payload = trace_payload.lower() not in ("0", "false", "no")

    perm_timeout = os.environ.get("WEAVE_PERMISSION_TIMEOUT_S")
    if perm_timeout is not None:
        try:
            perm_timeout_val = float(perm_timeout)
            if perm_timeout_val < 0:
                raise SystemExit(
                    f"Config error: WEAVE_PERMISSION_TIMEOUT_S must be >= 0, got: {perm_timeout!r}"
                )
            config.permission.timeout_s = perm_timeout_val
        except ValueError:
            raise SystemExit(
                f"Config error: WEAVE_PERMISSION_TIMEOUT_S must be a number, got: {perm_timeout!r}"
            )

    compact_auto = os.environ.get("WEAVE_COMPACT_AUTO")
    if compact_auto is not None:
        normalized = compact_auto.strip().lower()
        if normalized not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
            raise SystemExit(
                f"Config error: WEAVE_COMPACT_AUTO must be a boolean, got: {compact_auto!r}"
            )
        config.compaction.auto = normalized in ("1", "true", "yes", "on")

    compact_reserve = os.environ.get("WEAVE_COMPACT_RESERVE")
    if compact_reserve is not None:
        try:
            compact_reserve_val = int(compact_reserve)
            if compact_reserve_val < 0:
                raise SystemExit(
                    "Config error: WEAVE_COMPACT_RESERVE must be a non-negative integer, "
                    f"got: {compact_reserve!r}"
                )
            config.compaction.reserve_tokens = compact_reserve_val
        except ValueError:
            raise SystemExit(
                f"Config error: WEAVE_COMPACT_RESERVE must be an integer, got: {compact_reserve!r}"
            )

    compact_keep = os.environ.get("WEAVE_COMPACT_KEEP")
    if compact_keep is not None:
        try:
            compact_keep_val = int(compact_keep)
            if compact_keep_val < 0:
                raise SystemExit(
                    "Config error: WEAVE_COMPACT_KEEP must be a non-negative integer, "
                    f"got: {compact_keep!r}"
                )
            config.compaction.keep_tokens = compact_keep_val
        except ValueError:
            raise SystemExit(
                f"Config error: WEAVE_COMPACT_KEEP must be an integer, got: {compact_keep!r}"
            )

    compact_tool_limit = os.environ.get("WEAVE_COMPACT_TOOL_LIMIT")
    if compact_tool_limit is not None:
        try:
            compact_tool_limit_val = int(compact_tool_limit)
            if compact_tool_limit_val <= 0:
                raise SystemExit(
                    f"Config error: WEAVE_COMPACT_TOOL_LIMIT must be a positive integer, got: {compact_tool_limit!r}"
                )
            config.compaction.tool_result_limit = compact_tool_limit_val
        except ValueError:
            raise SystemExit(
                f"Config error: WEAVE_COMPACT_TOOL_LIMIT must be an integer, got: {compact_tool_limit!r}"
            )

    compact_tool_keep = os.environ.get("WEAVE_COMPACT_TOOL_KEEP")
    if compact_tool_keep is not None:
        try:
            compact_tool_keep_val = int(compact_tool_keep)
            if compact_tool_keep_val <= 0:
                raise SystemExit(
                    f"Config error: WEAVE_COMPACT_TOOL_KEEP must be a positive integer, got: {compact_tool_keep!r}"
                )
            config.compaction.tool_result_keep = compact_tool_keep_val
        except ValueError:
            raise SystemExit(
                f"Config error: WEAVE_COMPACT_TOOL_KEEP must be an integer, got: {compact_tool_keep!r}"
            )
