from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# prc请求消息格式
class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)

# 事件消息格式
class EventPushEnvelope(BaseModel):
    kind: Literal["event"] = "event"
    event: dict[str, Any]  # Event.model_dump() 的序列化结果


class JsonRpcSuccess(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str
    result: Any


class JsonRpcErrorObject(BaseModel):
    code: int
    message: str
    data: Any = None


class JsonRpcError(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | None = None
    error: JsonRpcErrorObject


PARSE_ERROR = -32700      # 解析错误
INVALID_REQUEST = -32600  # 请求格式错误
METHOD_NOT_FOUND = -32601 # 方法不存在
INVALID_PARAMS = -32602   # 参数错误
INTERNAL_ERROR = -32603   # 服务器内部错误
MESSAGE_TOO_LARGE = -32033  # 请求或响应超过 TCP 单帧限制
SERVER_BUSY = -32034  # 请求并发额度已满，可稍后重试
SERVER_SHUTTING_DOWN = -32035  # 已停止业务接入，不再登记普通请求


class HandlerError(Exception):
    """命令 handler 抛出此异常，SocketServer 将其转换为结构化 JSON-RPC 错误响应。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


# 构造一个 JSON-RPC 错误响应对象
def make_error(id: str | None, code: int, message: str, data: Any = None) -> JsonRpcError:
    return JsonRpcError(id=id, error=JsonRpcErrorObject(code=code, message=message, data=data))
