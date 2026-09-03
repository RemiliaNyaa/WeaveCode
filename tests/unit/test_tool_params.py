from __future__ import annotations

import pytest
from pydantic import ValidationError

from weavecode.core.tools.builtin.bash import BashParams
from weavecode.core.tools.builtin.edit_file import EditFileParams
from weavecode.core.tools.builtin.list_dir import ListDirParams
from weavecode.core.tools.builtin.read_file import ReadFileParams
from weavecode.core.tools.builtin.write_file import WriteFileParams


# 功能：验证 BashParams 接受合法参数，缺省 timeout 为 60
# 设计：直接 model_validate 字典，覆盖必填字段存在和可选字段默认值两种路径
def test_bash_params_valid() -> None:
    p = BashParams.model_validate({"command": "echo hi"})
    assert p.command == "echo hi"
    assert p.timeout == 60


# 功能：验证 BashParams timeout 上限被 pydantic le=120 约束
# 设计：传入 200 预期 ValidationError，确保不需要工具内部手动 min()
def test_bash_params_timeout_clamped() -> None:
    with pytest.raises(ValidationError):
        BashParams.model_validate({"command": "sleep 200", "timeout": 200})


# 功能：验证 BashParams 缺少 command 时抛 ValidationError
# 设计：传空字典触发 required 字段缺失，覆盖 schema_error 的核心触发条件
def test_bash_params_missing_command() -> None:
    with pytest.raises(ValidationError):
        BashParams.model_validate({})


# 功能：验证 BashParams command 为非字符串时抛 ValidationError
# 设计：传 int 触发类型校验，对应 agent 误传错误类型的情景
def test_bash_params_wrong_type() -> None:
    with pytest.raises(ValidationError):
        BashParams.model_validate({"command": 123})


# 功能：验证 BashParams 忽略额外字段（extra="ignore"）
# 设计：LLM 有时会多传字段，extra="ignore" 防止 ValidationError，保证鲁棒性
def test_bash_params_extra_ignored() -> None:
    p = BashParams.model_validate({"command": "ls", "unknown_field": "x"})
    assert p.command == "ls"


# 功能：验证 ReadFileParams 接受合法路径字符串，offset/limit 使用默认值
# 设计：最小合法输入，断言 path 原样保留、offset=1、limit=2000
def test_read_file_params_valid() -> None:
    p = ReadFileParams.model_validate({"path": "README.md"})
    assert p.path == "README.md"
    assert p.offset == 1
    assert p.limit == 2000


# 功能：验证 ReadFileParams 接受自定义 offset/limit
# 设计：传全部字段，断言三个字段均正确赋值
def test_read_file_params_with_offset_limit() -> None:
    p = ReadFileParams.model_validate({"path": "README.md", "offset": 100, "limit": 50})
    assert p.path == "README.md"
    assert p.offset == 100
    assert p.limit == 50


# 功能：验证 ReadFileParams path 为整数时抛 ValidationError
# 设计：覆盖 LLM 传 int 路径的异常场景，对应 schema_error 分类
def test_read_file_params_wrong_type() -> None:
    with pytest.raises(ValidationError):
        ReadFileParams.model_validate({"path": 42})


# 功能：验证 ReadFileParams offset 小于 1 时抛 ValidationError
# 设计：传 0 触发 ge=1 约束，覆盖"offset 从 1 开始"的不变量
def test_read_file_params_offset_zero() -> None:
    with pytest.raises(ValidationError):
        ReadFileParams.model_validate({"path": "x", "offset": 0})


# 功能：验证 WriteFileParams 需要 path 和 content 两个必填字段
# 设计：分别缺一个字段，覆盖两个 required 字段的独立缺失路径
def test_write_file_params_missing_fields() -> None:
    with pytest.raises(ValidationError):
        WriteFileParams.model_validate({"path": "out.txt"})  # missing content
    with pytest.raises(ValidationError):
        WriteFileParams.model_validate({"content": "hello"})  # missing path


# 功能：验证 WriteFileParams 合法输入原样保留
# 设计：完整输入，断言两个字段均正确赋值
def test_write_file_params_valid() -> None:
    p = WriteFileParams.model_validate({"path": "out.txt", "content": "hello"})
    assert p.path == "out.txt"
    assert p.content == "hello"


# 功能：验证 EditFileParams 需要 path、old_string、new_string 三个必填字段
# 设计：分别缺一个字段，覆盖三个 required 字段的独立缺失路径
def test_edit_file_params_missing_fields() -> None:
    with pytest.raises(ValidationError):
        EditFileParams.model_validate({"path": "a.txt", "old_string": "x"})  # missing new_string
    with pytest.raises(ValidationError):
        EditFileParams.model_validate({"path": "a.txt", "new_string": "y"})  # missing old_string
    with pytest.raises(ValidationError):
        EditFileParams.model_validate({"old_string": "x", "new_string": "y"})  # missing path


# 功能：验证 EditFileParams 合法输入原样保留，且无 replaceAll 字段
# 设计：完整输入断言三字段赋值正确；extra="ignore" 下传 replaceAll 会被忽略而非报错
def test_edit_file_params_valid() -> None:
    p = EditFileParams.model_validate(
        {"path": "a.txt", "old_string": "foo", "new_string": "bar"}
    )
    assert p.path == "a.txt"
    assert p.old_string == "foo"
    assert p.new_string == "bar"
    assert not hasattr(p, "replace_all")


# 功能：验证 EditFileParams 忽略额外字段（如历史 replaceAll 参数）
# 设计：LLM 若传 replaceAll，extra="ignore" 保证不报错，保持鲁棒性
def test_edit_file_params_extra_ignored() -> None:
    p = EditFileParams.model_validate(
        {"path": "a.txt", "old_string": "foo", "new_string": "bar", "replaceAll": True}
    )
    assert p.path == "a.txt"
    assert p.old_string == "foo"
    assert p.new_string == "bar"


# 功能：验证 ListDirParams path 必填、page 默认 1
# 设计：只传 path，断言 page 走默认值 1；传自定义 page 断言正确赋值
def test_list_dir_params_defaults() -> None:
    p = ListDirParams.model_validate({"path": "C:/x"})
    assert p.path == "C:/x"
    assert p.page == 1


# 功能：验证 ListDirParams page 小于 1 时抛 ValidationError
# 设计：传 0 触发 ge=1 约束，覆盖"页码从 1 开始"的不变量
def test_list_dir_params_page_zero() -> None:
    with pytest.raises(ValidationError):
        ListDirParams.model_validate({"path": "C:/x", "page": 0})


# 功能：验证 ListDirParams 缺少 path 时抛 ValidationError
# 设计：传空字典触发 required 字段缺失，覆盖空调用场景
def test_list_dir_params_missing_path() -> None:
    with pytest.raises(ValidationError):
        ListDirParams.model_validate({})
