from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class TraceRecord(BaseModel):
    ts: str
    direction: Literal[
        "CLIENT→CORE", # 客户端向core发送的请求
        "CORE→CLIENT", # core向客户端的请求发送的响应
        "CORE", # core内部的事件
        "CORE→LLM", # core调用llm api
        "LLM→CORE", # llm api调用的回复结果
    ]
    layer: Literal["ipc", "event", "llm"]
    kind: str  # command / response / error / push / event / api_call / api_response
    run_id: str | None = None
    step: int | None = None
    client_id: str | None = None
    data: dict[str, Any]
