"""Conversation state, commands and reply formatting (no Teams or Azure imports).

State is kept in memory per Teams conversation, which is fine for a single replica.
For more replicas, store it in Cosmos DB or Blob Storage keyed by conversation id.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

MAX_HISTORY_TURNS = 10  # user+assistant pairs sent back to the stateless runtime
MAX_HISTORY_CHARS = 6000  # per assistant message kept in history

HELP = (
    "Ask me a security question and I'll run an agent on it, for example "
    "*Prioritise CVE-2024-3400 and CVE-2023-4966*.\n\n"
    "- **agents**: the agents you can use\n"
    "- **me**: your tier, team, cost center and token limits\n"
    "- **use &lt;agent&gt;**: switch agent\n"
    "- **new**: start a new conversation\n"
    "- **signout**: sign out\n\n"
    "Your access and token limits come from your Entra groups."
)


@dataclass
class Conversation:
    conversation_id: str
    agent: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)
    epoch: int = 0  # bumped by "new"; part of the session id

    @property
    def session_id(self) -> str:
        """Telemetry session id: stable per conversation and epoch, safe for the gateway."""
        digest = hashlib.sha256(f"{self.conversation_id}|{self.epoch}".encode()).hexdigest()
        return f"teams-{digest[:24]}"

    def remember(self, question: str, answer: str) -> None:
        self.history += [
            {"role": "user", "content": question[:MAX_HISTORY_CHARS]},
            {"role": "assistant", "content": answer[:MAX_HISTORY_CHARS]},
        ]
        self.history = self.history[-2 * MAX_HISTORY_TURNS :]

    def reset(self) -> None:
        self.history = []
        self.epoch += 1


class ConversationStore:
    def __init__(self) -> None:
        self._items: dict[str, Conversation] = {}

    def get(self, conversation_id: str) -> Conversation:
        if conversation_id not in self._items:
            self._items[conversation_id] = Conversation(conversation_id)
        return self._items[conversation_id]


@dataclass(frozen=True)
class Command:
    name: str  # help | agents | use | new | signout | run
    arg: str = ""


def parse(text: str) -> Command:
    """Interpret a message. Anything that isn't a command is a question for the agent."""
    clean = re.sub(r"<at>.*?</at>", "", text or "").strip()
    lowered = clean.lower()
    if lowered in ("help", "/help", "?"):
        return Command("help")
    if lowered in ("agents", "/agents"):
        return Command("agents")
    if lowered in ("me", "/me", "whoami"):
        return Command("me")
    if lowered in ("new", "/new", "reset"):
        return Command("new")
    if lowered in ("signout", "/signout", "sign out"):
        return Command("signout")
    match = re.fullmatch(r"/?use\s+([a-z0-9][a-z0-9-]{0,62})", lowered)
    if match:
        return Command("use", match.group(1))
    return Command("run", clean)


def format_profile(g: dict[str, Any]) -> str:
    user = g.get("user", {})
    limits = g.get("limits", {})
    mine = limits.get("user", {})
    return (
        f"**{user.get('name') or user.get('upn') or user.get('id')}**\n\n"
        f"- Tier: **{g.get('tier')}** ({mine.get('tokensPerMinute', 0):,}/min, "
        f"{mine.get('tokensPerDay', 0):,}/day, {mine.get('tokensPerMonth', 0):,}/month)\n"
        f"- Team: **{g.get('team')}**, cost center **{g.get('costCenter') or '-'}** "
        f"(pool {limits.get('teamTokensPerMonth', 0):,}/month)\n"
        f"- Agents: {', '.join(g.get('agents', [])) or 'none'}"
    )


def format_answer(agent: str, body: dict[str, Any]) -> str:
    usage = body.get("usage", {})
    tools = ", ".join(t.get("operation") or t.get("name", "?") for t in body.get("tool_calls", []))
    footer = f"{agent} · {usage.get('total_tokens', 0):,} tokens"
    if tools:
        footer += f" · tools: {tools}"
    if body.get("stop_reason") not in (None, "completed"):
        footer += f" · stopped: {str(body['stop_reason']).replace('_', ' ')}"
    return f"{body.get('output', '')}\n\n_{footer}_"


def format_error(status: int, body: Any) -> str:
    error = body.get("error", {}) if isinstance(body, dict) else {}
    code = error.get("code", f"http_{status}")
    message = error.get("message") or "The agent gateway returned an error."
    if code in ("rate_limited", "quota_exceeded"):
        wait = error.get("retry_after")
        when = f" Try again in about {_human(wait)}." if wait else ""
        limit = error.get("limit")
        return f"⏳ {message}{when}" + (f" (limit: {limit})" if limit else "")
    if code in ("not_entitled", "no_tier"):
        return f"🔒 {message}"
    return f"⚠️ {message} ({code})"


def _human(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return f"{seconds} seconds"
    if s < 90:
        return f"{s} seconds"
    if s < 5400:
        return f"{round(s / 60)} minutes"
    return f"{round(s / 3600)} hours"
