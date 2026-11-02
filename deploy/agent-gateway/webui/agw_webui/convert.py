"""Translate between OpenAI Chat Completions (what Open WebUI speaks) and agent runs.

Pure functions only: the server in ``app.py`` does the I/O.
"""

from __future__ import annotations

import re
from typing import Any

MAX_TEXT = 20_000  # the runtime's limit per message
MAX_HISTORY = 40  # the runtime's limit on earlier turns
SESSION = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# Open WebUI stores the "thinking" panel inside the message as <details type="reasoning">;
# it is our run summary, not part of the conversation, so it never goes back to the agent.
DETAILS = re.compile(r"<details\b[^>]*>.*?</details>\s*", re.DOTALL | re.IGNORECASE)

LIMIT_LABELS = {
    "user_minute": "you, this minute",
    "user_daily": "you, today",
    "user_monthly": "you, this month",
    "team_monthly": "your team, this month",
    "agent_daily": "this agent, today",
    "run": "this run",
}


def message_text(content: Any) -> str:
    """Text of an OpenAI message ``content``: a string or a list of parts (images dropped)."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    else:
        text = ""
    return DETAILS.sub("", text).strip()


def split_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """(input, history) for a run from an OpenAI ``messages`` list.

    The last user message is the input; earlier user and assistant turns are the history.
    System messages are dropped: each agent has its own instructions.
    """
    turns = [
        {"role": m["role"], "content": message_text(m.get("content"))[:MAX_TEXT]}
        for m in messages
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]
    turns = [t for t in turns if t["content"]]
    if not turns or turns[-1]["role"] != "user":
        raise ValueError("The conversation must end with a user message.")
    return turns[-1]["content"], turns[:-1][-MAX_HISTORY:]


def session_id(chat_id: str | None) -> str | None:
    """Open WebUI's chat id as the gateway session id, if it is a valid one."""
    return chat_id if chat_id and SESSION.match(chat_id) else None


def run_summary(run: dict[str, Any]) -> str:
    """Markdown for the thinking panel: tools called, tokens, and remaining limits."""
    lines = []
    for call in run.get("tool_calls") or []:
        mark = "✓" if call.get("ok") else f"✗ {call.get('error_code') or 'error'}"
        lines.append(
            f"- `{call.get('operation') or call.get('name')}` {mark} · {call.get('ms', 0)} ms"
        )
    if not lines:
        lines.append("- no tools called")
    usage = run.get("usage") or {}
    lines.append("")
    lines.append(
        f"**{usage.get('total_tokens', 0):,} tokens** ({usage.get('prompt_tokens', 0):,} in, "
        f"{usage.get('completion_tokens', 0):,} out, {usage.get('cached_tokens', 0):,} cached) "
        f"in {usage.get('model_calls', 0)} model calls · stopped: {run.get('stop_reason', '?')}"
    )
    limits = run.get("limits") or {}
    if limits:
        lines.append("")
        lines.append("Tokens left:")
        for key, label in LIMIT_LABELS.items():
            if key in limits:
                lines.append(f"- {label}: {limits[key]:,}")
    else:
        lines.append("")
        lines.append("No gateway limits (local runtime).")
    return "\n".join(lines)


def error_text(status: int, body: Any) -> str:
    """A gateway or runtime error as a chat reply, so the user sees why."""
    error = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    message = error.get("message") or (body.get("message") if isinstance(body, dict) else None)
    message = message or f"HTTP {status}"
    code = error.get("code", "")
    if status == 429 or code in ("rate_limited", "quota_exceeded"):
        return f"⏳ {message}"
    if status in (401, 403):
        return f"🔒 {message}" + (f" ({code})" if code else "")
    return f"⚠️ The agent gateway returned an error: {message}" + (f" ({code})" if code else "")


def openai_usage(run: dict[str, Any] | None) -> dict[str, int]:
    usage = (run or {}).get("usage") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
        "total_tokens": int(usage.get("total_tokens", 0)),
    }


def completion(
    completion_id: str, model: str, created: int, content: str, usage: dict[str, int]
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def chunk(
    completion_id: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        out["usage"] = usage
    return out


def model_list(agents: list[dict[str, Any]]) -> dict[str, Any]:
    """Agents as OpenAI models, so each agent appears in Open WebUI's model picker."""
    return {
        "object": "list",
        "data": [
            {
                "id": a["name"],
                "object": "model",
                "created": 0,
                "owned_by": "agent-gateway",
                "name": a["name"],
                "description": a.get("description", ""),
            }
            for a in agents
        ],
    }
