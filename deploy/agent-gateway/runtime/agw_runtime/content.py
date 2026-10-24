"""What content the runtime may record in telemetry, and how it is redacted.

Spans carry metadata only (who, which agent, tokens, tool names, outcomes, timings).
When ``AGW_CAPTURE_CONTENT=true``, the runtime also records the user's input and each
tool call's arguments as **span events**. Azure Monitor stores span events in the
``AppTraces`` table, linked to their span, so content can have a shorter retention
than the spans themselves (30 days vs 90 in the Bicep).

Model outputs and tool results are never recorded.

Arguments listed in ``AGW_REDACT_ARGS`` (``operation.parameter``, comma-separated) are
replaced with ``"[redacted]"``. The default redacts Azure Resource Graph queries, which
can describe internal infrastructure.
"""

from __future__ import annotations

import json
import os
from typing import Any

MAX_CHARS = 4000
DEFAULT_REDACT = "azure.query.query"


def capture_enabled() -> bool:
    return os.environ.get("AGW_CAPTURE_CONTENT", "").lower() in ("1", "true", "yes", "on")


def _redactions() -> dict[str, set[str]]:
    rules: dict[str, set[str]] = {}
    for item in os.environ.get("AGW_REDACT_ARGS", DEFAULT_REDACT).split(","):
        operation, _, param = item.strip().rpartition(".")
        if operation and param:
            rules.setdefault(operation, set()).add(param)
    return rules


def truncate(text: str, limit: int = MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_arguments(operation: str | None, raw: str) -> str:
    """The tool call's arguments as compact JSON, with sensitive parameters redacted."""
    try:
        params: Any = json.loads(raw or "{}")
    except ValueError:
        return truncate(raw or "")
    if isinstance(params, dict) and operation:
        hidden = _redactions().get(operation, set())
        params = {k: ("[redacted]" if k in hidden else v) for k, v in params.items()}
    return truncate(json.dumps(params, ensure_ascii=False, separators=(",", ":")))
