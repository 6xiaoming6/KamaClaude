from pydantic import BaseModel

MAX_LINE_BYTES = 64 * 1024 * 1024


class FrameTooLargeError(ConnectionError):
    pass


# 接收与发送使用同一字节上限；换行分隔符不计入消息正文。
def encode_frame(message: BaseModel, limit: int = MAX_LINE_BYTES) -> bytes:
    payload = message.model_dump_json().encode("utf-8")
    if len(payload) > limit:
        raise FrameTooLargeError(f"IPC message exceeds {limit} bytes")
    return payload + b"\n"
