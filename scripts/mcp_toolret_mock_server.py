"""Local-only ToolRet metadata wrapper; never executes original API/code."""
from __future__ import annotations

import hashlib
import json
import keyword
import re
import sys
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field


def build_server(catalog_path: Path, server_name: str) -> MCPServer:
    server = MCPServer(server_name, log_level="ERROR")
    definitions = [d for d in json.loads(catalog_path.read_bytes()) if d["server"] == server_name]
    if not definitions:
        raise ValueError("Unknown local mock shard")
    types = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}
    for definition in definitions:
        name = definition["name"]
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and not keyword.iskeyword(name)
        required = [(n, s) for n, s in definition["parameters"].items() if s["required"]]
        optional = [(n, s) for n, s in definition["parameters"].items() if not s["required"]]
        params = []
        for key, spec in [*required, *optional]:
            assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and not keyword.iskeyword(key)
            annotation = types[spec["type"]] + (" | None" if not spec["required"] else "")
            params.append(f"{key}: Annotated[{annotation}, Field(description=_descriptions[{key!r}])]" +
                          (" = None" if not spec["required"] else ""))
        values = ", ".join(f"{key!r}: {key}" for key, _ in [*required, *optional])

        def execute(arguments: dict, tool_name: str = name) -> dict:
            payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
            return {"synthetic": True, "server": server_name, "tool": tool_name,
                    "arguments": arguments, "result_id": hashlib.sha256(payload.encode()).hexdigest()[:16],
                    "message": "Fabricated routing-only result. No external API or user file accessed."}

        namespace = {"Annotated": Annotated, "Field": Field, "Any": Any, "_execute": execute,
                     "_descriptions": {n: s["description"] for n, s in definition["parameters"].items()}}
        # Only validated identifiers and fixed type expressions enter generated source.
        # Descriptions are namespace data; original ToolRet example code is never evaluated.
        source = f"def {name}({', '.join(params)}) -> dict[str, Any]:\n    return _execute({{{values}}})\n"
        exec(source, namespace)
        server.add_tool(namespace[name], name=name, description=definition["description"])
    return server


if __name__ == "__main__":
    build_server(Path(sys.argv[1]).resolve(), sys.argv[2]).run(transport="stdio")
