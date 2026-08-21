from __future__ import annotations

from pathlib import Path

import pytest

from weavecode.core.skills.loader import SkillLoader

# 功能：内建 review skill 应能被 SkillLoader 查找到
# 设计：直接调用 resolve("review")，不依赖文件系统之外的任何状态
def test_builtin_skill_found() -> None:
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.name == "review"
    assert "审查" in skill.description or "review" in skill.description.lower()
    assert skill.path != ""


# 功能：内建 init / summarize / orchestrate skill 均可找到
# 设计：列举所有内建 skill 名，断言均能解析
@pytest.mark.parametrize("name", ["init_rules", "review", "summarize", "orchestrate"])

def test_all_builtin_skills_found(name: str) -> None:
    loader = SkillLoader()
    skill = loader.resolve(name)
    assert skill is not None, f"builtin skill '{name}' not found"


# 功能：不存在的 skill 名应返回 None
# 设计：查找一个不存在的名称，断言 resolve 返回 None 而非抛异常

# 功能：不存在的 skill 名应返回 None
# 设计：查找一个不存在的名称，断言 resolve 返回 None 而非抛异常
def test_unknown_skill_returns_none() -> None:
    loader = SkillLoader()
    result = loader.resolve("nonexistent_skill_xyz")
    assert result is None


# 功能：frontmatter 中的 name / description 应被正确解析，并记录绝对路径
# 设计：构造含 frontmatter 的 Markdown 文件，通过 _parse_skill_file 解析并验证结果

# 功能：frontmatter 中的 name / description 应被正确解析，并记录绝对路径
# 设计：构造含 frontmatter 的 Markdown 文件，通过 _parse_skill_file 解析并验证结果
def test_frontmatter_parsed(tmp_path: Path) -> None:
    from weavecode.core.skills.loader import _parse_skill_file

    content = """\
---
name: custom
description: 自定义 skill 测试
---
你是一个测试助手。
"""
    p = tmp_path / "custom.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "custom"
    assert skill.description == "自定义 skill 测试"
    assert skill.path == str(p.resolve())


# 功能：无 frontmatter 的 Markdown 文件仍可加载
# 设计：写入纯正文 Markdown，断言解析成功，name 用文件名、description 为空

# 功能：无 frontmatter 的 Markdown 文件仍可加载
# 设计：写入纯正文 Markdown，断言解析成功，name 用文件名、description 为空
def test_no_frontmatter(tmp_path: Path) -> None:
    from weavecode.core.skills.loader import _parse_skill_file

    content = "你是助手，请帮助用户完成任务。\n"
    p = tmp_path / "plain.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "plain"
    assert skill.description == ""
    assert skill.path == str(p.resolve())


# 功能：项目本地 skill 应覆盖内建同名 skill
# 设计：在 .weave/skills/ 中写入同名文件，用 monkeypatch 修改 cwd，断言加载到的是本地版本

# 功能：项目本地 skill 应覆盖内建同名 skill
# 设计：在 .weave/skills/ 中写入同名文件，用 monkeypatch 修改 cwd，断言加载到的是本地版本
def test_project_overrides_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_skills = tmp_path / ".weave" / "skills"
    local_skills.mkdir(parents=True)
    (local_skills / "review.md").write_text(
        "---\nname: review\ndescription: local override\n---\nlocal system prompt\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.description == "local override"
    assert skill.path == str((tmp_path / ".weave" / "skills" / "review.md").resolve())


# 功能：render_catalog 生成 skill 清单（格式 B：- name: desc (file: 绝对路径)）
# 设计：内建 skill 存在时，清单包含技能名与 file: 绝对路径
