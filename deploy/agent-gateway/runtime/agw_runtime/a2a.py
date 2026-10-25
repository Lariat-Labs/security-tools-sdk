"""Agent2Agent (A2A) protocol adapter, so Copilot Studio (and other A2A clients) can use the agents.

Copilot Studio connects to an "A2A agent" by endpoint URL. It sends JSON-RPC 2.0 requests
(``method: "message/send"``, the A2A ``Message`` in ``params``) to that URL and expects a JSON
reply whose ``result`` is an A2A ``Message``. It also passes the conversation so far in
``message.metadata["copilotstudio.microsoft.com/a2a/chathistory"]`` and a stable
``contextId`` per conversation.

The agent card (name, description, skills) is served at ``<endpoint>/.well-known/agent.json``
(the URL Copilot Studio reads) and ``agent-card.json`` (A2A 0.3's name). It is public: it's what
a client reads before anyone signs in.

The runs themselves go through the gateway's front door like any other run, so the caller's
Entra token, entitlements, token limits, metrics and usage events all apply.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from .agents import AgentDefinition

PROTOCOL_VERSION = "0.3.0"
CHAT_HISTORY_KEY = "copilotstudio.microsoft.com/a2a/chathistory"
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")

# JSON-RPC 2.0 error codes (and A2A's range for application errors)
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
INTERNAL_ERROR = -32603


class A2AError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def agent_card(agent: AgentDefinition, endpoint: str) -> dict[str, Any]:
    """An A2A agent card. ``endpoint`` is the public URL of the agent's A2A endpoint."""
    tools = sorted(agent.tool_names())
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": agent.name,
        "description": agent.description,
        "url": endpoint,
        "preferredTransport": "JSONRPC",
        "version": "1.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": agent.name,
                "name": agent.name,
                "description": f"{agent.description} Uses: {', '.join(tools)}.",
                "tags": ["security", *agent.providers],
            }
        ],
    }


def parse_request(body: Any) -> tuple[Any, str, list[dict[str, str]], str]:
    """(request id, user text, history, context id) from a JSON-RPC ``message/send`` request."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        raise A2AError(INVALID_REQUEST, "Expected a JSON-RPC 2.0 request.")
    if body["method"] not in ("message/send", "message/stream"):
        raise A2AError(
            METHOD_NOT_FOUND, f"Method {body['method']!r} is not supported; use message/send."
        )
    message = (body.get("params") or {}).get("message")
    if not isinstance(message, dict):
        raise A2AError(INVALID_PARAMS, "params.message is required.")
    text = "\n".join(
        str(p.get("text", ""))
        for p in message.get("parts") or []
        if isinstance(p, dict) and p.get("kind", "text") == "text" and p.get("text")
    ).strip()
    if not text:
        raise A2AError(INVALID_PARAMS, "The message has no text part.")
    context_id = str(message.get("contextId") or "")
    return body.get("id"), text[:20_000], chat_history(message, text), context_id


def chat_history(message: dict[str, Any], current: str) -> list[dict[str, str]]:
    """Copilot Studio's chat history as runtime ``history`` (earlier turns only, last 20)."""
    raw = (message.get("metadata") or {}).get(CHAT_HISTORY_KEY) or []
    entries: list[dict[str, Any]] = []
    for item in raw:  # either a flat list of entries or [{"HasValue": true, "Value": [...]}]
        if isinstance(item, dict) and isinstance(item.get("Value"), list):
            entries.extend(e for e in item["Value"] if isinstance(e, dict))
        elif isinstance(item, dict):
            entries.append(item)
    turns = []
    for e in entries:
        text = str(e.get("Text") or "").strip()
        if not text:
            continue
        sender = str(e.get("From") or "").lower()
        role = "user" if sender in ("", "user") else "assistant"
        turns.append({"role": role, "content": text[:6000]})
    # The current message is usually the last history entry too.
    if turns and turns[-1]["role"] == "user" and turns[-1]["content"].strip() == current.strip():
        turns = turns[:-1]
    # The runtime expects alternating context; drop leading assistant greetings.
    while turns and turns[0]["role"] == "assistant":
        turns = turns[1:]
    return turns[-20:]


def session_from_context(context_id: str) -> str:
    """A conversation id the gateway's telemetry accepts, from A2A's contextId."""
    clean = re.sub(r"[^A-Za-z0-9-]", "", context_id)[:64]
    return clean if SESSION_PATTERN.match(clean or "") else ""


def reply(
    request_id: Any, text: str, context_id: str, *, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "kind": "message",
        "role": "agent",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    if context_id:
        message["contextId"] = context_id
    if metadata:
        message["metadata"] = metadata
    return {"jsonrpc": "2.0", "id": request_id, "result": message}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
