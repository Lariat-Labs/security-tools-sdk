"""Export operations as LLM tool definitions.

The agent in this project drives ``secctl`` rather than importing the SDK, but
these schemas are still useful: ``secctl schema`` prints them so an agent (or an
MCP server, or a function-calling loop) can see every operation's exact inputs.
"""

from __future__ import annotations

from typing import Any, Literal

from .operation import Operation
from .registry import Registry, default_registry

Style = Literal["anthropic", "openai", "plain"]


def tool_definition(op: Operation, style: Style = "anthropic") -> dict[str, Any]:
    description = op.summary
    if op.description:
        description += "\n\n" + op.description
    if op.auth:
        description += "\n\nRequires credentials for this provider."
    schema = op.input_schema()
    if style == "anthropic":
        return {"name": op.tool_name, "description": description, "input_schema": schema}
    if style == "openai":
        return {
            "type": "function",
            "function": {"name": op.tool_name, "description": description, "parameters": schema},
        }
    return {"operation": op.fqname, "description": description, "input_schema": schema}


def tool_definitions(
    registry: Registry | None = None, style: Style = "anthropic", providers: list[str] | None = None
) -> list[dict[str, Any]]:
    reg = registry or default_registry()
    return [
        tool_definition(op, style)
        for op in reg.operations()
        if not providers or op.provider in providers
    ]
