from __future__ import annotations


class ToolError(Exception):
    # 只用于能够确定没有未知副作用的参数、读取和业务前置错误
    def __init__(self, message: str, error_type: str = "invalid_operation") -> None:
        super().__init__(message)
        self.error_type = error_type


class RateLimitedError(Exception):
    """Raised by a tool when the upstream service is rate-limiting the request."""
