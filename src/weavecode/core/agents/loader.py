from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# 一个子 Agent 角色的定义：对外描述、系统提示与工具白名单
@dataclass
class AgentProfile:
    name: str
    description: str = ""
    system_prompt: str = ""
    allowed_tools: list[str] = field(default_factory=list)


# 按三级优先级（项目本地 > 用户全局 > 内建）查找角色 TOML 并解析
class AgentProfileLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 项目本地路径：基于当前 cwd resolve 成绝对路径（依赖 daemon 启动 cwd）
    @staticmethod
    def _project_dir() -> Path:
        return Path(".weave/agents").resolve()

    # 全局路径：expanduser 展开成绝对路径
    @staticmethod
    def _global_dir() -> Path:
        return Path("~/.weave/agents").expanduser()

    # 三个角色目录（项目本地 > 全局 > 内置）
    def _dirs(self) -> list[Path]:
        return [self._project_dir(), self._global_dir(), self._BUILTIN_DIR]

    # 按优先级查找角色定义文件；未找到返回 None
    def resolve(self, name: str) -> AgentProfile | None:
        for d in self._dirs():
            path = d / f"{name}.toml"
            if not path.exists():
                continue
            try:
                return self._parse(name, path)
            except Exception:
                log.warning("agent profile '%s' is broken: %s", name, path, exc_info=True)
                return None
        return None

    # 解析 [agent] 小节里的描述、系统提示与工具白名单
    @staticmethod
    def _parse(name: str, path: Path) -> AgentProfile:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        section = data.get("agent", {})
        allowed = section.get("allowed_tools") or []
        return AgentProfile(
            name=name,
            description=str(section.get("description", "")),
            system_prompt=str(section.get("system_prompt", "")),
            allowed_tools=[str(tool) for tool in allowed],
        )
