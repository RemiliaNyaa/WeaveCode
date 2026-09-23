from __future__ import annotations

from pathlib import Path

import pytest

from weavecode.core.config import get_config


def _write_env(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# 功能：验证 .env 文件中的值被正确加载并覆盖内建默认值
# 设计：写 .env 到临时目录并 chdir 进去，清除同名系统环境变量排除干扰，确认 .env 加载路径有效
def test_dotenv_base_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    _write_env(env_file, "WEAVE_PORT=9999\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEAVE_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 9999


# 功能：验证系统环境变量的优先级高于 .env 文件中的值
# 设计：.env 写 9999，系统环境变量写 8888，确认最终值为 8888，对应四级优先链的顶层约束
def test_system_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    _write_env(env_file, "WEAVE_PORT=9999\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEAVE_PORT", "8888")

    cfg = get_config()

    assert cfg.port == 8888


# 功能：验证 .env 文件不存在时静默跳过，使用内建默认值（不抛异常）
# 设计：chdir 到空目录，清除系统环境变量，确认 get_config() 不因 .env 缺失而崩溃，默认端口为 7437
def test_missing_env_file_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEAVE_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 7437


# 功能：验证 .env 中设置的 WEAVE_CONFIG 能正确影响 JSON 配置文件的加载路径
# 设计：.env 指向自定义 JSON 文件，JSON 中写入不同端口，确认 .env 在 JSON 加载前被读取（优先级链的正确顺序）
def test_dotenv_before_json_weave_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    json_path = tmp_path / "custom.json"
    json_path.write_text('{"core": {"port": 5555}}', encoding="utf-8")

    env_file = tmp_path / ".env"
    _write_env(env_file, f"WEAVE_CONFIG={json_path}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEAVE_CONFIG", raising=False)
    monkeypatch.delenv("WEAVE_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 5555


# 功能：验证同一变量经过完整四级优先链后，最终值为最高优先级来源（系统环境变量）
# 设计：同时设置默认值(7437)/JSON(6000)/.env(7000)/系统环境变量(8000)，确认最终值为 8000，是优先级链的综合正确性验证
def test_priority_chain_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 默认值：7437
    # JSON：6000
    # .env：7000
    # 系统环境变量：8000（最高）
    json_path = tmp_path / "weave.json"
    json_path.write_text('{"core": {"port": 6000}}', encoding="utf-8")

    env_file = tmp_path / ".env"
    _write_env(env_file, "WEAVE_PORT=7000\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEAVE_CONFIG", str(json_path))
    monkeypatch.setenv("WEAVE_PORT", "8000")

    cfg = get_config()

    assert cfg.port == 8000


# 功能：JSON 配置里的 agent.repeat_limit 能被读到（重复调用闸门的阈值可配）
# 设计：把 WEAVE_CONFIG 指向临时 JSON 并清掉同名环境变量，确认「配置文件」这条来源真的生效
def test_agent_repeat_limit_from_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    json_path = tmp_path / "weave.json"
    json_path.write_text('{"agent": {"repeat_limit": 7}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEAVE_CONFIG", str(json_path))
    monkeypatch.delenv("WEAVE_REPEAT_LIMIT", raising=False)

    cfg = get_config()

    assert cfg.agent.repeat_limit == 7


# 功能：环境变量 WEAVE_REPEAT_LIMIT 的优先级高于 JSON 配置
# 设计：JSON 写 7、环境变量写 9，断言最终为 9——锁住「环境变量在四级优先链顶层」这条约束
def test_agent_repeat_limit_env_overrides_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_path = tmp_path / "weave.json"
    json_path.write_text('{"agent": {"repeat_limit": 7}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEAVE_CONFIG", str(json_path))
    monkeypatch.setenv("WEAVE_REPEAT_LIMIT", "9")

    cfg = get_config()

    assert cfg.agent.repeat_limit == 9


# 功能：JSON 配置里的 mcp.refreshOnNotify 能被读到（默认 true，可关掉通知驱动的刷新）
# 设计：显式写 false 并断言生效——默认值 true 时无法区分「读到了」还是「用了默认」，必须写非默认值
def test_mcp_refresh_on_notify_from_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_path = tmp_path / "weave.json"
    json_path.write_text('{"mcp": {"refreshOnNotify": false}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEAVE_CONFIG", str(json_path))
    monkeypatch.delenv("WEAVE_MCP_REFRESH_ON_NOTIFY", raising=False)

    cfg = get_config()

    assert cfg.mcp.refresh_on_notify is False


# 功能：环境变量 WEAVE_MCP_REFRESH_ON_NOTIFY 的优先级高于 JSON 配置
# 设计：JSON 写 false、环境变量写 true，断言最终为 true——锁住「环境变量在四级优先链顶层」
def test_mcp_refresh_on_notify_env_overrides_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_path = tmp_path / "weave.json"
    json_path.write_text('{"mcp": {"refreshOnNotify": false}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEAVE_CONFIG", str(json_path))
    monkeypatch.setenv("WEAVE_MCP_REFRESH_ON_NOTIFY", "true")

    cfg = get_config()

    assert cfg.mcp.refresh_on_notify is True


# 功能：JSON 配置里的 llm.stream_retries 能被读到（流式重试次数可配）
# 设计：写一个非默认值（3）并断言生效——用默认值 5 无法区分「读到了」还是「用了默认」
def test_llm_stream_retries_from_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_path = tmp_path / "weave.json"
    json_path.write_text('{"llm": {"stream_retries": 3}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEAVE_CONFIG", str(json_path))
    monkeypatch.delenv("WEAVE_LLM_STREAM_RETRIES", raising=False)

    cfg = get_config()

    assert cfg.llm.stream_retries == 3


# 功能：环境变量 WEAVE_LLM_STREAM_RETRIES 的优先级高于 JSON 配置
# 设计：JSON 写 3、环境变量写 7，断言最终为 7——锁住「环境变量在四级优先链顶层」
def test_llm_stream_retries_env_overrides_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_path = tmp_path / "weave.json"
    json_path.write_text('{"llm": {"stream_retries": 3}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEAVE_CONFIG", str(json_path))
    monkeypatch.setenv("WEAVE_LLM_STREAM_RETRIES", "7")

    cfg = get_config()

    assert cfg.llm.stream_retries == 7
