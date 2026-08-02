from __future__ import annotations

from pathlib import Path

# 规则文件路径
POLICY_FILE = str(Path.home() / ".weave" / "policy.toml")


# 读出规则文件 [always] 表里的 工具名 → 决策 映射；文件不存在按空规则处理
def load_policy(path: str = POLICY_FILE) -> dict[str, str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    rules: dict[str, str] = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "always" or "=" not in line:
            continue
        key, _, val = line.partition("=")
        rules[key.strip().strip('"')] = val.strip().strip('"')
    return rules


# 把 工具名 → 决策 映射写回规则文件
def save_policy_file(rules: dict[str, str], path: str = POLICY_FILE) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[always]"]
    for tool in sorted(rules):
        lines.append(f'{tool} = "{rules[tool]}"')
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
