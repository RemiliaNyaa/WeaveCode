from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str
    # SKILL.md 绝对路径
    path: str
    # 正文模板，触发时按参数渲染后作为本次 run 的目标
    system_prompt_template: str = ""


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# 解析 Markdown skill 文件：frontmatter 取 name/description，正文作模板
def _parse_skill_file(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    name = path.stem
    description = ""
    abs_path = str(path.resolve())

    m = _FRONTMATTER_RE.match(text)
    template = text[m.end():] if m else text
    if m:
        lines = m.group(1).splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("name:"):
                name = stripped[len("name:"):].strip().strip('"').strip("'")
            elif stripped.startswith("description:"):
                val = stripped[len("description:"):].strip().strip('"').strip("'")
                # YAML 块标量：> (折叠) 或 | (保留换行)，后续缩进行是内容
                if val in (">", "|"):
                    fold = val == ">"
                    parts: list[str] = []
                    i += 1
                    while i < len(lines) and (
                        lines[i].startswith(" ") or lines[i].startswith("\t")
                    ):
                        parts.append(lines[i].strip())
                        i += 1
                    description = (" ".join(parts) if fold else "\n".join(parts)).strip()
                    continue
                description = val
            i += 1

    return Skill(
        name=name,
        description=description,
        path=abs_path,
        system_prompt_template=template,
    )


# 按三级优先级（项目本地 > 用户全局 > 内建）查找并解析 skill
class SkillLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 项目本地路径：基于当前 cwd resolve 成绝对路径（依赖 daemon 启动 cwd）
    @staticmethod
    def _project_dir() -> Path:
        return Path(".weave/skills").resolve()

    # 全局路径：expanduser 展开成绝对路径
    @staticmethod
    def _global_dir() -> Path:
        return Path("~/.weave/skills").expanduser()

    # 三个 skill 目录（项目本地 > 全局 > 内置）
    def _dirs(self) -> list[Path]:
        return [self._project_dir(), self._global_dir(), self._BUILTIN_DIR]

    # 按优先级查找 skill 文件；未找到返回 None
    def resolve(self, name: str) -> Skill | None:
        for path in self._search_paths(name):
            if path.exists():
                try:
                    return _parse_skill_file(path)
                except Exception:
                    return None
        return None

    # 返回候选路径列表，同时支持扁平文件（name.md）和目录式（name/SKILL.md）两种格式
    def _search_paths(self, name: str) -> list[Path]:
        paths: list[Path] = []
        for d in self._dirs():
            paths.append(d / f"{name}.md")
            paths.append(d / name / "SKILL.md")
        return paths

    # 列出所有可用 skill 名称（内建 + 用户全局 + 项目本地，去重后以项目本地覆盖为准）
    def list_all(self) -> list[str]:
        seen: dict[str, None] = {}
        for d in self._dirs():
            if d.exists():
                for f in sorted(d.glob("*.md")):
                    seen[f.stem] = None
                for f in sorted(d.glob("*/SKILL.md")):
                    seen[f.parent.name] = None
        return list(seen)

    # 列出所有可用 Skill 对象（含描述），项目本地覆盖同名内建
    def list_all_skills(self) -> list[Skill]:
        seen: dict[str, Skill] = {}
        for d in self._dirs():
            if d.exists():
                for f in sorted(d.glob("*.md")):
                    try:
                        skill = _parse_skill_file(f)
                        seen[skill.name] = skill
                    except Exception:
                        pass
                for f in sorted(d.glob("*/SKILL.md")):
                    try:
                        skill = _parse_skill_file(f)
                        seen[skill.name] = skill
                    except Exception:
                        pass
        return list(seen.values())

    # 生成系统提示词里的 skill 清单段（每行一个：- 名字: 描述）
    def render_catalog(self) -> str:
        skills = self.list_all_skills()
        if not skills:
            return "当前没有可用技能。"
        lines = []
        for skill in sorted(skills, key=lambda s: s.name):
            desc = skill.description.splitlines()[0] if skill.description else ""
            lines.append(f"- {skill.name}: {desc}")
        return "\n".join(lines)

    # 渲染 skill 正文：把 $ARGUMENTS 占位符换成调用时传入的参数
    def render_prompt(self, skill: Skill, arguments: str) -> str:
        return skill.system_prompt_template.replace("$ARGUMENTS", arguments)
