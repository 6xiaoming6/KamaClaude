from __future__ import annotations

import asyncio
import os
import sys
import threading


# 使用原始 IO，避免 input() 持有 stdin 的缓冲锁，导致解释器退出仍等它解锁。
def _read_line() -> str:
    if os.name == "nt" and sys.stdin.isatty():
        return _read_windows_console()
    fd = os.dup(sys.stdin.fileno())
    content = bytearray()
    try:
        while chunk := os.read(fd, 1):
            content.extend(chunk)
            if chunk == b"\n":
                break
    finally:
        os.close(fd)
    if not content:
        raise EOFError
    return content.decode(sys.stdin.encoding or "utf-8").removesuffix("\n").removesuffix("\r")


def _read_windows_console() -> str:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    read = kernel.ReadConsoleW
    read.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    read.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    parts: list[str] = []
    while True:
        buffer = ctypes.create_unicode_buffer(4096)
        count = wintypes.DWORD()
        if not read(handle, buffer, 4096, ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not count.value:
            raise EOFError
        part = "".join(buffer[:count.value])
        if part.startswith("\x1a"):
            raise EOFError
        parts.append(part)
        if part.endswith("\n"):
            return "".join(parts).removesuffix("\n").removesuffix("\r")


# stdin 可能一直阻塞；独立守护线程不占默认线程池，断线退出不用等 input 结束。
async def readline(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    print(prompt, end="", flush=True)

    def deliver(value: str | BaseException) -> None:
        if future.done():
            return
        if isinstance(value, BaseException):
            future.set_exception(value)
        else:
            future.set_result(value)

    def read() -> None:
        try:
            value: str | BaseException = _read_line()
        except (EOFError, OSError, ValueError) as exc:
            value = exc
        try:
            loop.call_soon_threadsafe(deliver, value)
        except RuntimeError:
            pass  # 前端已经退出，不向已关闭的事件循环交付迟到输入。

    threading.Thread(target=read, name="kama-cli-input", daemon=True).start()
    return await future
