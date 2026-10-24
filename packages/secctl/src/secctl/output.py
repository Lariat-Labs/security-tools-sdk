"""Rendering results: JSON (default, for agents and scripts), JSONL, or tables (for humans)."""

from __future__ import annotations

import json
import sys
from typing import Any

from secsdk.result import Result, to_jsonable

FORMATS = ("json", "jsonl", "table")


def project(data: Any, fields: list[str]) -> Any:
    """Keep only ``fields`` from a dict or from each dict in a list (also inside items/rows)."""
    if not fields:
        return data
    if isinstance(data, list):
        return [project(d, fields) for d in data]
    if isinstance(data, dict):
        for key in ("items", "rows"):
            if isinstance(data.get(key), list) and not any(f in data for f in fields):
                return {**data, key: project(data[key], fields)}
        return {k: v for k, v in data.items() if k in fields}
    return data


def emit(
    result: Result, fmt: str = "json", fields: list[str] | None = None, compact: bool = False
) -> int:
    payload = result.to_dict()
    if result.ok and fields:
        payload["data"] = project(payload["data"], fields)

    if fmt == "table":
        _emit_table(payload)
    elif fmt == "jsonl":
        data = payload.get("data")
        if result.ok and isinstance(data, list):
            for row in data:
                sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        indent = None if compact else 2
        sys.stdout.write(json.dumps(payload, indent=indent, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return result.exit_code


def emit_value(
    operation: str, data: Any, fmt: str, fields: list[str] | None = None, compact: bool = False
) -> int:
    """Emit a locally produced value (ops listing, schemas...) using the same envelope."""
    return emit(Result(ok=True, operation=operation, data=to_jsonable(data)), fmt, fields, compact)


# ----- tables ---------------------------------------------------------------------------


def _cell(value: Any, width: int = 80) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        text = ", ".join(_cell(v, 40) for v in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _emit_table(payload: dict[str, Any]) -> None:
    from rich.console import Console
    from rich.table import Table

    out = Console()
    if not out.is_terminal:
        out = Console(width=200)  # piped output: don't squeeze tables to 80 columns
    err = Console(stderr=True)
    if not payload["ok"]:
        e = payload["error"]
        err.print(f"[bold red]error[/] [{e.get('code')}] {e.get('message')}")
        if e.get("hint"):
            err.print(f"[yellow]hint:[/] {e['hint']}")
        if e.get("details"):
            err.print(_cell(e["details"], 400))
        return

    def table_for(rows: list[dict[str, Any]], title: str | None = None) -> Table:
        cols: list[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        t = Table(title=title, show_lines=False, header_style="bold")
        for c in cols:
            t.add_column(c, overflow="ellipsis", no_wrap=True, max_width=48)
        for r in rows:
            t.add_row(*(_cell(r.get(c), 48) for c in cols))
        return t

    def render(data: Any, title: str | None = None) -> None:
        if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
            out.print(table_for(data, title))
        elif isinstance(data, dict):
            scalars = {
                k: v
                for k, v in data.items()
                if not (isinstance(v, list) and v and isinstance(v[0], dict))
            }
            if scalars:
                t = Table(title=title, show_header=False)
                t.add_column("field", style="bold")
                t.add_column("value", overflow="fold")
                for k, v in scalars.items():
                    t.add_row(k, _cell(v, 200))
                out.print(t)
            for k, v in data.items():
                if k not in scalars:
                    render(v, k)
        elif isinstance(data, list) and not data:
            out.print(f"[dim]{title or 'result'}: no results[/]")
        else:
            out.print(data)

    render(payload.get("data"))
    meta = payload.get("meta", {})
    if meta:
        err.print(
            f"[dim]{payload['operation']} · "
            + " · ".join(f"{k}={v}" for k, v in meta.items())
            + "[/]"
        )
