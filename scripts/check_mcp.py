from __future__ import annotations

import argparse
import asyncio
import json
import os

from kama_claude.core.mcp.client import McpClient


# 使用项目客户端检查 Cloudflare 公共 MCP 的发现、搜索和关闭，不调用模型
async def check(args: argparse.Namespace) -> None:
    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    saved = {key: os.environ.get(key) for key in proxy_keys}
    if args.direct:
        for key in proxy_keys:
            os.environ.pop(key, None)
    client = McpClient()
    try:
        await client.connect_http(args.url)
        tools = await client.list_tools()
        if args.tool not in {tool.name for tool in tools}:
            raise RuntimeError(f"tool not found: {args.tool}")
        result = await client.call_tool(args.tool, {"query": args.query})
        if not result.strip():
            raise RuntimeError("MCP returned empty search content")
        report = {
            "url": args.url,
            "protocol_version": client.protocol_version,
            "tools": [tool.name for tool in tools],
            "called_tool": args.tool,
            "query": args.query,
            "result_chars": len(result),
            "result_preview": result[:300],
        }
    finally:
        await client.close()
        if args.direct:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
    report["closed"] = True
    print(json.dumps(report, ensure_ascii=False, indent=2))


# 提供可复现的真实远程服务检查命令，直连只影响当前检查进程
def main() -> None:
    parser = argparse.ArgumentParser(description="Check a real Streamable HTTP MCP service")
    parser.add_argument("--url", default="https://docs.mcp.cloudflare.com/mcp")
    parser.add_argument("--tool", default="search_cloudflare_documentation")
    parser.add_argument("--query", default="What is Cloudflare Workers?")
    parser.add_argument("--direct", action="store_true", help="ignore proxy env for this check only")
    asyncio.run(check(parser.parse_args()))


if __name__ == "__main__":
    main()
