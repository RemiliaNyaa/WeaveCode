from __future__ import annotations

from pathlib import Path

import pytest

from weavecode.core.agents.loader import AgentProfile, AgentProfileLoader


# 写一份角色定义文件到项目本地目录
def _write_profile(root: Path, name: str, body: str) -> Path:
    d = root / ".weave" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.toml"
    p.write_text(body, encoding="utf-8")
    return p


# 功能：验证能读取角色定义文件并解析出描述、系统提示与工具白名单
# 设计：写一份带 [agent] 小节的 TOML，断言四个字段逐个落地，路径为解析后的绝对路径
def test_profile_toml_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_profile(
        tmp_path,
        "explorer",
        "[agent]\n"
        'description = "只读探索角色"\n'
        'system_prompt = "你是代码库探索助手。"\n'
        'allowed_tools = ["read_file", "grep"]\n',
    )

    loader = AgentProfileLoader()
    profile = loader.resolve("explorer")

    assert profile is not None
    assert profile.name == "explorer"
    assert profile.description == "只读探索角色"
    assert profile.system_prompt == "你是代码库探索助手。"
    assert profile.allowed_tools == ["read_file", "grep"]
    assert (tmp_path / ".weave" / "agents" / "explorer.toml").resolve().is_file()


# 功能：验证角色文件缺字段时用缺省值兜底
# 设计：只有 [agent] 空小节，断言描述、系统提示与白名单都回退到默认值，不抛异常
def test_profile_missing_fields_use_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, "plain", "[agent]\n")

    loader = AgentProfileLoader()
    profile = loader.resolve("plain")

    assert profile is not None
    assert isinstance(profile, AgentProfile)
    assert profile.name == "plain"
    assert profile.description == ""
    assert profile.system_prompt == ""
    assert profile.allowed_tools == []


# 功能：验证不存在的角色名返回 None
# 设计：查找一个不存在的名称，断言 resolve 返回 None 而非抛异常
def test_unknown_profile_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    loader = AgentProfileLoader()
    assert loader.resolve("nonexistent_profile_xyz") is None


# 功能：验证项目本地角色覆盖用户全局同名角色
# 设计：两级目录各写一份同名文件，断言加载到的是项目本地那份
def test_project_profile_overrides_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    global_root = tmp_path / "global"
    _write_profile(project, "helper", "[agent]\ndescription = \"项目本地角色\"\n")
    _write_profile(global_root, "helper", "[agent]\ndescription = \"用户全局角色\"\n")

    monkeypatch.chdir(project)
    monkeypatch.setattr(
        AgentProfileLoader, "_global_dir", staticmethod(lambda: global_root / ".weave" / "agents")
    )

    loader = AgentProfileLoader()
    profile = loader.resolve("helper")

    assert profile is not None
    assert profile.description == "项目本地角色"


# 功能：验证角色文件损坏时返回 None 而不是把异常抛给调用方
# 设计：写一段非法 TOML，断言解析失败被吞掉、resolve 返回 None
def test_broken_toml_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, "broken", "[agent\ndescription = 不合法\n")

    loader = AgentProfileLoader()
    assert loader.resolve("broken") is None
