from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str
    # SKILL.md 绝对路径（清单渲染 + 手动触发提示用）
    path: str

    # 手动触发时注入的 user 消息：告知"用户想用哪个 skill + 路径 + 任务"，
    # 让 agent 按系统提示规则用 read_file 读取 SKILL.md 正文
    def user_prompt(self, task: str) -> str:
        return (
            f"用户想要使用 skill：{self.name}\n"
            f"skill.md 路径：{self.path}\n"
            f"用户任务：{task}"
        )


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# 解析 Markdown skill 文件，提取 frontmatter 的 name/description 和绝对路径
def _parse_skill_file(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    name = path.stem
    description = ""
    abs_path = str(path.resolve())

    m = _FRONTMATTER_RE.match(text)
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
                    while i < len(lines) and (lines[i].startswith(" ") or lines[i].startswith("\t")):
                        parts.append(lines[i].strip())
                        i += 1
                    description = (" ".join(parts) if fold else "\n".join(parts)).strip()
                    continue
                else:
                    description = val
            i += 1

    return Skill(
        name=name,
        description=description,
        path=abs_path,
    )


# 按三级优先级（项目本地 > 用户全局 > 内建）查找并解析 skill
class SkillLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 接受会话工作目录；未指定时项目本地回退到当前进程 cwd
    def __init__(self, working_dir: str | None = None) -> None:
        self._working_dir = working_dir

    # 项目本地路径：基于会话工作目录解析（未指定时回退到当前 cwd）
    def _project_dir(self) -> Path:
        if self._working_dir:
            return (Path(self._working_dir) / ".weave" / "skills").resolve()
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

    # 生成系统提示词里的 skill 清单段（格式 B：- name: desc (file: 绝对路径)）
    def render_catalog(self) -> str:
        skills = self.list_all_skills()
        lines = [
            "可用技能列表如下：",
            "",
        ]
        if not skills:
            lines.append("当前没有可用技能。")
        else:
            for skill in sorted(skills, key=lambda s: s.name):
                desc = skill.description.splitlines()[0] if skill.description else ""
                lines.append(f"- {skill.name}: {desc} (file: {skill.path})")
        lines += [
            "",
            "使用规则：",
            "1. 只有当任务【清晰匹配】某个技能的描述时，才使用该技能；"
            "否则直接正常完成任务，不要强行套用技能。",
            "2. 若用户明确点名某技能（如 @技能名 或 /技能名），则必须使用该技能。",
            "3. 同时匹配多个技能时，选择覆盖请求的【最少】技能组合。",
            "4. 决定使用某技能后，先用 read_file 读取该技能 SKILL.md 的完整内容，"
            "再按其中指令执行。",
            "5. 若某技能看似可用但无法干净应用（缺文件/指令不清），"
            "简要说明后选择次优方案继续，不要卡住。",
        ]
        return "\n".join(lines)