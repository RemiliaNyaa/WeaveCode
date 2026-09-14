from __future__ import annotations

from pathlib import Path

# 持久化规则：(project, permission, resource) 三元组
Rule = tuple[str, str, str]

# 规则文件路径
POLICY_FILE = str(Path.home() / ".weave" / "policy.toml")

# 写文件时带上的文件头说明
_FILE_HEADER = (
    "# ~/.weave/policy.toml\n"
    "# 由 weave-core 自动管理，手动编辑生效但格式须正确\n"
    "# 每条记录 = 某项目下、某权限、某资源已被「始终允许」"
)

# 一条记录的三个键
_RECORD_KEYS = ("project", "permission", "resource")


# 读出规则文件里的全部记录；缺文件按空记录，缺键的记录整条丢弃
def load_policy(path: str = POLICY_FILE) -> list[Rule]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    rules: list[Rule] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[[allow]]":
            if len(current) == len(_RECORD_KEYS):
                rules.append(
                    (current["project"], current["permission"], current["resource"])
                )
            current = {}
            continue
        if "[" in line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = _unquote(key.strip())
        if key in _RECORD_KEYS:
            current[key] = _unquote(val.strip())
    if len(current) == len(_RECORD_KEYS):
        rules.append((current["project"], current["permission"], current["resource"]))
    return rules


# 把记录数组整体写回规则文件
def save_policy_file(rules: list[Rule], path: str = POLICY_FILE) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [_FILE_HEADER]
    for project, permission, resource in rules:
        lines.append("[[allow]]")
        lines.append(f"project = {_quote(project)}")
        lines.append(f"permission = {_quote(permission)}")
        lines.append(f"resource = {_quote(resource)}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


# 用双引号包起字符串并转义反斜杠与引号，保证路径写回去后还能原样读回
def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# 去掉包裹字符串的引号并还原转义；没有引号的裸值原样返回
def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        body = value[1:-1]
        if value[0] == '"':
            return body.replace('\\"', '"').replace("\\\\", "\\")
        return body
    return value
