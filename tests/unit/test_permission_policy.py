from __future__ import annotations

import os

from weavecode.core.permissions.command_safety import is_dangerous_command
from weavecode.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    evaluate_hard,
    evaluate_soft,
    param_preview,
)
from weavecode.core.permissions.shell_paths import extract_shell_paths

# ── evaluate_hard: 危险命令黑名单（硬编码） ──────────────────────────────────

# 功能：验证危险命令在 hard 层返回 ASK（强制审批，不可被缓存绕过）
# 设计：rm -rf / sudo / find -exec / 写文件重定向 命中危险黑名单；普通命令不受影响
def test_hard_dangerous_command_forces_ask() -> None:
    policy = ToolPolicy(default=PermissionDecision.ALLOW)
    assert evaluate_hard("bash", {"command": "rm -rf /tmp"}, policy) == PermissionDecision.ASK
    assert evaluate_hard("bash", {"command": "sudo ls"}, policy) == PermissionDecision.ASK
    assert evaluate_hard("bash", {"command": "find . -exec rm {} ;"}, policy) == PermissionDecision.ASK
    assert evaluate_hard("bash", {"command": "echo hi > /etc/x"}, policy) == PermissionDecision.ASK
    assert evaluate_hard("bash", {"command": "ls -la"}, policy) is None


# 功能：验证越界不由 evaluate_hard 处理（已移交 tree-sitter + external_directory）
# 设计：绝对路径命令在 hard 层返回 None，越界判定统一由 extract_shell_paths 承担
def test_hard_no_longer_handles_outside_cwd() -> None:
    policy = ToolPolicy(default=PermissionDecision.ALLOW)
    assert evaluate_hard("bash", {"command": "cat /etc/hosts"}, policy) is None


# 功能：验证非 bash 工具无硬规则
# 设计：只有 bash 有命令内容；read_file 等即使路径越界也不在 hard 层判定
def test_hard_non_bash_no_rules() -> None:
    policy = ToolPolicy(default=PermissionDecision.ALLOW)
    assert evaluate_hard("read_file", {"path": "/etc/hosts"}, policy) is None


# ── is_dangerous_command: 危险命令识别 ───────────────────────────────────────

# 功能：验证强制删除 / 提权 / 磁盘操作被识别为危险
# 设计：覆盖 rm 的多种 flag 写法与 sudo/su/dd/mkfs
def test_dangerous_rm_and_privilege() -> None:
    assert is_dangerous_command("rm -rf /tmp/x")
    assert is_dangerous_command("rm -f a.txt")
    assert is_dangerous_command("rm -fr build")
    assert is_dangerous_command("rm --force a.txt")
    assert is_dangerous_command("sudo rm -rf /")
    assert is_dangerous_command("su -")
    assert is_dangerous_command("dd if=/dev/zero of=/dev/sda")
    assert is_dangerous_command("mkfs.ext4 /dev/sda1")
    assert not is_dangerous_command("rm a.txt")          # 无 -f
    assert not is_dangerous_command("ls -la")


# 功能：验证 find / ln 的危险参数被识别
# 设计：find -exec/-delete 可执行任意命令；ln -s 建符号链接
def test_dangerous_find_and_ln() -> None:
    assert is_dangerous_command("find . -exec rm {} ;")
    assert is_dangerous_command("find /data -delete")
    assert is_dangerous_command("ln -s /etc/passwd ./p")
    assert not is_dangerous_command("find . -name '*.py'")
    assert not is_dangerous_command("ln a b")            # 无 -s


# 功能：验证复合命令里任一子命令危险即整体危险
# 设计：&&、管道、子 shell 内的命令都会被语法树遍历找到
def test_dangerous_compound_commands() -> None:
    assert is_dangerous_command("echo ok && rm -rf y")
    assert is_dangerous_command("ls | sudo tee /etc/x")
    assert is_dangerous_command("(rm -rf x)")
    assert not is_dangerous_command("echo ok && ls -la")


# 功能：验证透明前缀（env / nohup / xargs）剥掉后仍能识别内层危险命令
# 设计：这些命令自身无害，但会执行其后的命令
def test_dangerous_transparent_wrappers() -> None:
    assert is_dangerous_command("env rm -rf x")
    assert is_dangerous_command("nohup rm -rf x")
    assert is_dangerous_command("xargs rm -rf")
    assert is_dangerous_command("env FOO=1 rm -rf x")    # env 的 VAR=VAL 也要剥掉
    assert not is_dangerous_command("env ls")


# 功能：验证 shell 启动器会递归检查内层脚本
# 设计：bash -c / sh -c 后的脚本文本递归解析，防止绕过
def test_dangerous_shell_launcher_recursion() -> None:
    assert is_dangerous_command('bash -lc "rm -rf /tmp/x"')
    assert is_dangerous_command("sh -c 'sudo ls'")
    assert not is_dangerous_command('bash -lc "ls -la"')


# 功能：验证写文件重定向被识别为危险（> / >>），而无害重定向不算
# 设计：shell 重定向可绕过 write_file 的越界检查，必须拦；2>&1 / >/dev/null 放行
def test_dangerous_redirect() -> None:
    assert is_dangerous_command("echo hi > /etc/passwd")
    assert is_dangerous_command("echo hi >> out.txt")
    assert is_dangerous_command("cmd 2> /tmp/err")
    assert not is_dangerous_command("cmd > /dev/null")
    assert not is_dangerous_command("cmd 2>&1")
    assert not is_dangerous_command("echo hi")


# ── extract_shell_paths: tree-sitter 路径提取 ────────────────────────────────

# 功能：验证能提取绝对路径参数并判定为工作目录之外
# 设计：cat 是路径命令白名单成员，/etc/passwd 应被提取为绝对路径
def test_shell_paths_absolute() -> None:
    got = extract_shell_paths("cat /etc/passwd", "/proj")
    assert got == [os.path.normpath("/etc/passwd")]


# 功能：验证相对路径按工作目录归一化后仍在目录内
# 设计：相对路径不越界（normalize_path 以 working_dir 为基准拼接）
def test_shell_paths_relative_stays_inside() -> None:
    got = extract_shell_paths("cat notes.txt", "/proj")
    assert got == [os.path.normpath("/proj/notes.txt")]


# 功能：验证 ~ 被展开为家目录
# 设计：~/.ssh 属于家目录，越出工作目录
def test_shell_paths_tilde_expands() -> None:
    got = extract_shell_paths("cat ~/.ssh/id_rsa", "/proj")
    assert got == [os.path.normpath(str(__import__("pathlib").Path.home() / ".ssh" / "id_rsa"))]


# 功能：验证非白名单命令不提取路径（避免把普通参数误判为路径）
# 设计：echo 不是路径命令，其参数不应被当作路径
def test_shell_paths_non_path_command_ignored() -> None:
    assert extract_shell_paths("echo /etc/hosts", "/proj") == []


# 功能：验证动态内容（$VAR、命令替换）被保守跳过
# 设计：无法静态解析的值不应误判为目录内，也不应误报（交给 bash 工具级审批兜底）
def test_shell_paths_dynamic_skipped() -> None:
    assert extract_shell_paths("cat $SECRET", "/proj") == []
    assert extract_shell_paths("cat $(pwd)/x", "/proj") == []


# 功能：验证通配符参数被截断到前缀
# 设计：ls /data/*.txt → 提取 /data/，从而判定越界
def test_shell_paths_glob_prefix() -> None:
    got = extract_shell_paths("ls /data/*.txt", "/proj")
    assert got == [os.path.normpath("/data")]


# 功能：验证管道与 && 链式命令中的路径都能被提取
# 设计：语法树遍历覆盖 pipeline / list 等复合结构，不漏检
def test_shell_paths_pipeline_and_chain() -> None:
    got = extract_shell_paths("cat /etc/hosts | grep localhost && cat /var/log/x", "/proj")
    assert os.path.normpath("/etc/hosts") in got
    assert os.path.normpath("/var/log/x") in got


# 功能：验证引号包裹的路径被正确去引号
# 设计：cat "/data/a b.txt" → 提取含空格的绝对路径
def test_shell_paths_quoted() -> None:
    got = extract_shell_paths('cat "/data/a b.txt"', "/proj")
    assert got == [os.path.normpath("/data/a b.txt")]


# ── evaluate_soft: 默认策略（DEFAULT_POLICIES）────────────────────────────────

# 功能：验证 evaluate_soft 直接返回该工具的默认策略
# 设计：工具层默认全放行（对齐 opencode），软规则不再读任何名单
def test_soft_returns_tool_default() -> None:
    assert evaluate_soft("bash", {"command": "echo hi"}, DEFAULT_POLICIES["bash"]) == PermissionDecision.ALLOW
    assert evaluate_soft("bash", {"command": "echo hi"}, ToolPolicy(default=PermissionDecision.ASK)) == PermissionDecision.ASK


# 功能：验证 bash / write_file 默认策略是 ALLOW（工具层默认放行，对齐 opencode）
# 设计：默认全放行，安全交给危险黑名单与越界层
def test_soft_bash_default_is_allow() -> None:
    assert evaluate_soft("bash", {"command": "echo hi"}, DEFAULT_POLICIES["bash"]) == PermissionDecision.ALLOW


# 功能：验证 read_file / list_dir 等只读工具默认策略是 ALLOW
# 设计：只读或安全工具默认不打扰用户，降低权限疲劳
def test_soft_safe_tools_default_allow() -> None:
    assert evaluate_soft("read_file", {"path": "README.md"}, DEFAULT_POLICIES["read_file"]) == PermissionDecision.ALLOW
    assert evaluate_soft("list_dir", {"path": "."}, DEFAULT_POLICIES["list_dir"]) == PermissionDecision.ALLOW


# 功能：验证 write_file 默认策略是 ALLOW（工具层不再逐次审批）
# 设计：写文件的位置风险由 external_directory 越界层负责
def test_soft_write_file_default_is_allow() -> None:
    assert evaluate_soft("write_file", {"path": "out.txt", "content": "hi"}, DEFAULT_POLICIES["write_file"]) == PermissionDecision.ALLOW


# ── param_preview ─────────────────────────────────────────────────────────────

# 功能：验证 param_preview 对已知工具返回 key='value' 格式的摘要
# 设计：TUI 审批卡片依赖这个摘要让用户快速理解工具要做什么，格式必须稳定
def test_param_preview_known_tools() -> None:
    assert param_preview("bash", {"command": "echo hi"}) == "command='echo hi'"
    assert param_preview("read_file", {"path": "README.md"}) == "path='README.md'"


# 功能：验证 param_preview 超出 60 字符时截断并加省略号
# 设计：避免审批卡片展示超长命令撑破 UI 布局
def test_param_preview_truncates_long_value() -> None:
    long_cmd = "echo " + "x" * 100
    preview = param_preview("bash", {"command": long_cmd})
    assert len(preview) <= 75  # key='<60 chars>…' overhead ~11 chars
    assert "…" in preview