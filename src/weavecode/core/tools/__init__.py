from weavecode.core.tools.base import BaseTool, ToolResult
from weavecode.core.tools.errors import RateLimitedError
from weavecode.core.tools.invocation import invoke_tool
from weavecode.core.tools.registry import ToolRegistry

# 错误分类常量：工具回填失败结果时统一用这几个取值
ERROR_RUNTIME = "runtime_error"
ERROR_TIMEOUT = "timeout"
ERROR_SCHEMA = "schema_error"
ERROR_PERMISSION_DENIED = "permission_denied"
ERROR_RATE_LIMITED = "rate_limited"

__all__ = [
    "BaseTool",
    "ERROR_PERMISSION_DENIED",
    "ERROR_RATE_LIMITED",
    "ERROR_RUNTIME",
    "ERROR_SCHEMA",
    "ERROR_TIMEOUT",
    "RateLimitedError",
    "ToolRegistry",
    "ToolResult",
    "invoke_tool",
]
