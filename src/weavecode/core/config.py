from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7437
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "~/.weave/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.weave/config.toml"
_DEFAULT_MAX_STEPS = 20
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


@dataclass
class LlmConfig:
    default_model: str = _DEFAULT_MODEL
    router: str = "static"  # "static" | "rule_based" | "cost_budget"


@dataclass
class TraceConfig:
    enabled: bool = True
    file: str = _DEFAULT_TRACE_FILE
    include_llm_payload: bool = True  # false 时 LLM 记录只保留摘要


@dataclass
class PermissionConfig:
    timeout_s: float = 60.0  # 审批超时秒数；0 表示不超时
    persist: bool = True  # 是否把「始终允许」的审批决定写入 policy.toml


@dataclass
class WeaveConfig:
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)


# 构建并返回运行时配置：默认值 → TOML → .env → WEAVE_* 环境变量（后者优先级最高）
def get_config() -> WeaveConfig:
    config = WeaveConfig()

    # .env 必须在读取 WEAVE_CONFIG 之前加载，以便 .env 中的 WEAVE_CONFIG 能影响 TOML 路径
    load_dotenv(".env", override=False)

    config_path = Path(os.environ.get("WEAVE_CONFIG", _DEFAULT_CONFIG_PATH)).expanduser()
    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        _apply_toml(config, data)

    _apply_env(config)
    return config


# 取出一个小节表：缺失按空表处理，类型不符直接退出并报出小节名
def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SystemExit(f"Config error: [{name}] must be a table")
    return value


# 取出字符串字段：缺失返回 None，类型不符直接退出并报出键名
def _read_str(section: dict[str, Any], key: str, path: str) -> str | None:
    if key not in section:
        return None
    value = section[key]
    if not isinstance(value, str):
        raise SystemExit(f"Config error: {path} must be a string")
    return value


# 取出整数字段：缺失返回 None，非整数直接退出并报出键名
def _read_int(section: dict[str, Any], key: str, path: str) -> int | None:
    if key not in section:
        return None
    value = section[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise SystemExit(f"Config error: {path} must be an integer")
    return value


# 取出浮点字段（整数也接受）：缺失返回 None，类型不符直接退出
def _read_float(section: dict[str, Any], key: str, path: str) -> float | None:
    if key not in section:
        return None
    value = section[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SystemExit(f"Config error: {path} must be a number")
    return float(value)


# 取出布尔字段：缺失返回 None，类型不符直接退出
def _read_bool(section: dict[str, Any], key: str, path: str) -> bool | None:
    if key not in section:
        return None
    value = section[key]
    if not isinstance(value, bool):
        raise SystemExit(f"Config error: {path} must be a boolean")
    return value


# 取出字符串列表字段：缺失返回 None，元素类型不符直接退出
def _read_str_list(section: dict[str, Any], key: str, path: str) -> list[str] | None:
    if key not in section:
        return None
    value = section[key]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise SystemExit(f"Config error: {path} must be an array of strings")
    return list(value)


# 取出字符串表字段：缺失返回 None，类型不符直接退出
def _read_str_table(section: dict[str, Any], key: str, path: str) -> dict[str, str] | None:
    if key not in section:
        return None
    value = section[key]
    if not isinstance(value, dict):
        raise SystemExit(f"Config error: {path} must be a table")
    return {str(k): str(v) for k, v in value.items()}


# 把 TOML 根表写入 config（只认已知小节，未知小节忽略）
def _apply_toml(config: WeaveConfig, data: dict[str, Any]) -> None:
    core = _section(data, "core")
    host = _read_str(core, "host", "core.host")
    if host is not None:
        config.host = host
    port = _read_int(core, "port", "core.port")
    if port is not None:
        config.port = port

    log = _section(data, "logging")
    level = _read_str(log, "level", "logging.level")
    if level is not None:
        config.logging.level = level
    log_file = _read_str(log, "file", "logging.file")
    if log_file is not None:
        config.logging.file = log_file
    log_format = _read_str(log, "format", "logging.format")
    if log_format is not None:
        config.logging.format = log_format

    agent = _section(data, "agent")
    max_steps = _read_int(agent, "max_steps", "agent.max_steps")
    if max_steps is not None:
        config.agent.max_steps = max_steps

    llm = _section(data, "llm")
    default_model = _read_str(llm, "default_model", "llm.default_model")
    if default_model is not None:
        config.llm.default_model = default_model
    router = _read_str(llm, "router", "llm.router")
    if router is not None:
        config.llm.router = router

    trace = _section(data, "trace")
    enabled = _read_bool(trace, "enabled", "trace.enabled")
    if enabled is not None:
        config.trace.enabled = enabled
    trace_file = _read_str(trace, "file", "trace.file")
    if trace_file is not None:
        config.trace.file = trace_file
    payload = _read_bool(trace, "include_llm_payload", "trace.include_llm_payload")
    if payload is not None:
        config.trace.include_llm_payload = payload

    perm = _section(data, "permission")
    timeout_s = _read_float(perm, "timeout_s", "permission.timeout_s")
    if timeout_s is not None:
        config.permission.timeout_s = timeout_s
    persist = _read_bool(perm, "persist", "permission.persist")
    if persist is not None:
        config.permission.persist = persist


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

    default_model = os.environ.get("WEAVE_LLM_DEFAULT_MODEL")
    if default_model is not None:
        config.llm.default_model = default_model

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

    perm_persist = os.environ.get("WEAVE_PERMISSION_PERSIST")
    if perm_persist is not None:
        if perm_persist.strip().lower() in ("1", "true", "yes", "on"):
            config.permission.persist = True
        elif perm_persist.strip().lower() in ("0", "false", "no", "off"):
            config.permission.persist = False
        else:
            raise SystemExit(
                f"Config error: WEAVE_PERMISSION_PERSIST must be a boolean, got: {perm_persist!r}"
            )
