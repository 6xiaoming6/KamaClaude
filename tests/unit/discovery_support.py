from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

from kama_claude.core.discovery.model import CatalogTool
from kama_claude.core.llm.provider import DeepseekProvider
from kama_claude.core.mcp.client import McpClient, McpToolDef


# 构造带真实 Schema 的工具记录，避免测试只有名称而没有参数信息。
def record(name="get_issue", description="Get GitHub issue details", default=1):
    return CatalogTool.from_definition("server-id", "github", {
        "name": name, "description": description,
        "inputSchema": {"type": "object", "properties": {
            "number": {"type": "integer", "description": "Issue number", "default": default},
        }},
    })


# 返回 MCP 边界测试桩，真正的 SQLite、检索与 Loop 仍照常运行。
def mcp_client():
    client = AsyncMock(spec=McpClient)
    client.list_tools.return_value = [McpToolDef(
        "get_issue", "Get GitHub issue details",
        {"type": "object", "properties": {"number": {"type": "integer"}}},
    )]
    client.call_tool.return_value = "Issue details"
    return client


class FakeEmbedder:
    # 三个方向是确定性的测试标签，不冒充真实语义模型。
    def __init__(self, key="fake-v1"):
        self.key = key
        self.encoded = 0
        self.queries = []

    # 模拟模型版本变化，检验缓存命名空间隔离。
    def fingerprint(self):
        return self.key

    # 测试集使用可检查的标签构造向量，方便断言召回与缓存行为。
    def _vector(self, text):
        lower = text.lower()
        if any(word in lower for word in ["issue", "缺陷", "缺点", "故障"]):
            return [1.0, 0.0, 0.0]
        if any(word in lower for word in ["database", "数据库", "sql", "数据表"]):
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    # 记录真正重算的条数，而不是以数据库写入次数冒充 embedding 次数。
    def documents(self, texts):
        self.encoded += len(texts)
        return [self._vector(text) for text in texts]

    # 记录原始输入，验证 Skill 展开与工具结果没有覆盖用户目标。
    def query(self, background, description):
        self.queries.append((background, description))
        return self._vector(description)


class ScriptedProvider:
    # 固定响应序列，保存模型实际收到的 Schema 快照。
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    # 完整深拷贝，防止之后的状态修改掩盖初次请求中的全量 Schema 泄露。
    async def chat(self, messages, tool_schemas, bus, run_id, **kwargs):
        self.requests.append(deepcopy({"messages": messages, "schemas": tool_schemas, **kwargs}))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class NativeScriptedProvider(DeepseekProvider):
    # 保留真实 Provider 身份，仅替换模型推理；序列化另由真实 SDK 测试覆盖。
    def __init__(self, responses):
        super().__init__("test-model", client=object())
        self.script = ScriptedProvider(responses)
        self.requests = self.script.requests

    async def chat(self, *args, **kwargs):
        return await self.script.chat(*args, **kwargs)
