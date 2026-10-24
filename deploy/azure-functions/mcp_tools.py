"""The logic behind the MCP tools: plain Python with no Azure Functions imports, so it is unit-tested.

The tools mirror the secctl CLI. An agent on MCP gets the same discovery verbs
(guide, providers, describe), the same generic `call`, and the same JSON
envelope that `secctl` prints. Agent instructions written for the CLI carry over.

Environment controls:

* ``SECSDK_MCP_ALLOWED_PROVIDERS``: comma list; only these providers are callable
  (default: all). Use it to give an internet-facing server only public sources.
* ``SECSDK_MCP_MAX_CHARS``: cap on a tool result's size (default 60,000).
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from secsdk import Client, CoreSettings, SecSDKError, UnknownOperationError, default_registry
from secsdk.operation import Operation
from secsdk.result import Result

_client: Client | None = None


def get_client() -> Client:
    """One Client per worker process, so connection pools and caches are reused."""
    global _client
    if _client is None:
        if os.environ.get("FUNCTIONS_WORKER_RUNTIME"):
            # Only the temp dir is reliably writable on Functions plans.
            os.environ.setdefault("SECSDK_CACHE_DIR", os.path.join(tempfile.gettempdir(), "secsdk"))
        _client = Client(CoreSettings())
    return _client


def _allowed_providers() -> set[str] | None:
    raw = os.environ.get("SECSDK_MCP_ALLOWED_PROVIDERS", "").strip()
    return {p.strip() for p in raw.split(",") if p.strip()} or None


def _visible_operations() -> list[Operation]:
    allowed = _allowed_providers()
    return [
        op for op in default_registry().operations() if allowed is None or op.provider in allowed
    ]


def _envelope(result: Result) -> str:
    text = json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))
    limit = int(os.environ.get("SECSDK_MCP_MAX_CHARS", "60000"))
    if len(text) > limit:
        cut = {
            "ok": result.ok,
            "operation": result.operation,
            "error": {
                "code": "result_too_large",
                "message": f"Result was {len(text)} characters (limit {limit}).",
                "hint": "Narrow the query: lower 'limit', shorten 'days', or add filters.",
            },
            "meta": result.meta,
        }
        return json.dumps(cut)
    return text


def _error(operation: str, exc: SecSDKError) -> str:
    return _envelope(Result(ok=False, operation=operation, error=exc.to_dict()))


# ----- tools --------------------------------------------------------------------------------


def call(operation: str, params: Any = None) -> str:
    """Run one operation; returns the secctl JSON envelope as a string."""
    operation = (operation or "").strip().replace("-", "_")
    if isinstance(params, str):
        try:
            params = json.loads(params) if params.strip() else {}
        except json.JSONDecodeError as exc:
            return _envelope(
                Result(
                    ok=False,
                    operation=operation,
                    error={"code": "invalid_input", "message": f"params is not valid JSON: {exc}"},
                )
            )
    params = params or {}
    if not isinstance(params, dict):
        return _envelope(
            Result(
                ok=False,
                operation=operation,
                error={"code": "invalid_input", "message": "params must be a JSON object"},
            )
        )
    allowed = _allowed_providers()
    provider = operation.partition(".")[0]
    if allowed is not None and provider not in allowed:
        return _error(
            operation,
            UnknownOperationError(
                f"Provider '{provider}' is not available on this server",
                hint=f"Available providers: {', '.join(sorted(allowed))}",
            ),
        )
    return _envelope(get_client().execute(operation, params))


def guide() -> str:
    """Markdown overview of every available operation and how to call it."""
    lines = [
        "# secsdk tools",
        "",
        "Call any operation with `secsdk_call(operation, params)` where params is a JSON object,",
        'e.g. `secsdk_call("kev.lookup", {"cve_id": "CVE-2023-4966"})`.',
        "Results are JSON: check `ok`; on failure read `error.code` and follow `error.hint`.",
        "`not_found` is a valid answer. `not_configured` means the server lacks credentials for that provider.",
        "Use `secsdk_describe(operation)` for full parameter details. All operations are read-only.",
        "",
    ]
    by_provider: dict[str, list[Operation]] = {}
    for op in _visible_operations():
        by_provider.setdefault(op.provider, []).append(op)
    reg = default_registry()
    for name, ops in by_provider.items():
        cls = reg.get(name)
        lines += [f"## {name}: {cls.title}", cls.description, ""]
        for op in ops:
            params = ", ".join(
                f"{p['name']}{'' if p['required'] else '?'}: {p['type']}" for p in op.parameters()
            )
            lock = " (needs credentials)" if op.auth else ""
            lines.append(f"- `{op.fqname}`({params}): {op.summary}{lock}")
        lines.append("")
    return "\n".join(lines)


def providers() -> str:
    client = get_client()
    allowed = _allowed_providers()
    rows = []
    for cls in client.registry.providers():
        if allowed is not None and cls.name not in allowed:
            continue
        status = client.provider(cls.name).auth_status()
        rows.append(
            {
                "provider": cls.name,
                "title": cls.title,
                "configured": status.configured,
                "missing": status.missing,
                "note": status.note,
            }
        )
    return _envelope(Result(ok=True, operation="secsdk.providers", data=rows))


def describe(operation: str) -> str:
    name = (operation or "").strip().replace("-", "_")
    try:
        op = default_registry().operation(name)
    except SecSDKError as exc:
        return _error(name, exc)
    allowed = _allowed_providers()
    if allowed is not None and op.provider not in allowed:
        return _error(
            name, UnknownOperationError(f"Provider '{op.provider}' is not available on this server")
        )
    return _envelope(Result(ok=True, operation="secsdk.describe", data=op.describe()))
