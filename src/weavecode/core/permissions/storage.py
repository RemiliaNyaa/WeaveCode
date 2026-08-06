from __future__ import annotations

from pathlib import Path

# 规则文件路径
POLICY_FILE = str(Path.home() / ".weave" / "policy.toml")

# 写文件时带上的文件头说明
_FILE_HEADER = (
    "# ~/.weave/policy.toml\n"
    "# 由 weave-core 自动管理，手动编辑生效但格式须正确\n"
    "# 每行 = 某工具已被「始终允许/始终拒绝」"
)

# 规则文件里只认这两个决策值，其余一律忽略
_VALID_DECISIONS = frozenset({"allow", "deny"})


# 读出规则文件 [always] 表里的 工具名 → 决策 映射；文件缺失、内容异常都按空规则处理
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
        tool = _unquote(key.strip())
        decision = _unquote(val.strip())
        if not tool or decision not in _VALID_DECISIONS:
            continue
        rules[tool] = decision
    return rules


# 把 工具名 → 决策 映射整体写回规则文件
def save_policy_file(rules: dict[str, str], path: str = POLICY_FILE) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [_FILE_HEADER, "[always]"]
    for tool in sorted(rules):
        if rules[tool] in _VALID_DECISIONS:
            lines.append(f'{tool} = "{rules[tool]}"')
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


# 去掉包裹字符串的引号并还原转义；没有引号的裸值原样返回
def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        body = value[1:-1]
        if value[0] == '"':
            return body.replace('\\"', '"').replace("\\\\", "\\")
        return body
    return value
