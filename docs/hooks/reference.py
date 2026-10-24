"""MkDocs hook: generate reference content from the live SDK registry.

Markers replaced at build time:

* ``<!-- secsdk:providers -->``  provider overview table
* ``<!-- secsdk:settings -->``   environment variables per provider
* ``<!-- secsdk:operations -->`` every operation with parameters and examples

Because the docs read the registry, a new provider shows up here with no doc edits.
"""

from __future__ import annotations

from typing import Any

from secctl.dynamic import usage_line
from secsdk import CoreSettings, default_registry
from secsdk.config import ProviderSettings
from secsdk.provider import Provider


def _auth_label(cls: type[Provider]) -> str:
    ops = cls.operations.values()
    if not any(op.auth for op in ops):
        return "none"
    if all(op.auth for op in ops):
        return "required"
    return "some operations"


def providers_table() -> str:
    rows = ["| Provider | Source | Credentials | Operations |", "|---|---|---|---|"]
    for cls in default_registry().providers():
        title = f"[{cls.title}]({cls.docs_url})" if cls.docs_url else cls.title
        ops = ", ".join(
            f"`{op.name}`" for op in sorted(cls.operations.values(), key=lambda o: o.name)
        )
        rows.append(
            f"| [`{cls.name}`](reference/operations.md#{cls.name}) | {title} | {_auth_label(cls)} | {ops} |"
        )
    return "\n".join(rows)


def _env_names(
    settings_cls: type[ProviderSettings] | type[CoreSettings], field: str, info: Any
) -> list[str]:
    alias = info.validation_alias
    if alias is not None and hasattr(alias, "choices"):
        return [str(c) for c in alias.choices]
    if isinstance(alias, str):
        return [alias]
    prefix = settings_cls.model_config.get("env_prefix", "")
    return [f"{prefix}{field}".upper()]


def _settings_rows(settings_cls: Any) -> list[str]:
    rows = []
    for field, info in settings_cls.model_fields.items():
        names = " / ".join(f"`{n}`" for n in _env_names(settings_cls, field, info))
        default = info.get_default(call_default_factory=True)
        default_s = "" if default in (None, [], "") else f"`{default}`"
        rows.append(f"| {names} | {default_s} | {info.description or ''} |")
    return rows


def settings_tables() -> str:
    out = ["### Core (all providers)", "", "| Variable | Default | Description |", "|---|---|---|"]
    out += _settings_rows(CoreSettings)
    out.append("")
    for cls in default_registry().providers():
        if cls.settings_class is None:
            continue
        out += [f"### {cls.name}", "", "| Variable | Default | Description |", "|---|---|---|"]
        out += _settings_rows(cls.settings_class)
        out.append("")
    return "\n".join(out)


def operations_reference() -> str:
    out: list[str] = []
    for cls in default_registry().providers():
        out += [f"## {cls.name}", "", f"**{cls.title}**: {cls.description}", ""]
        if cls.docs_url:
            out += [f"Upstream API docs: <{cls.docs_url}>", ""]
        for op in sorted(cls.operations.values(), key=lambda o: o.name):
            out += [f"### `{op.fqname}`", "", op.summary, ""]
            if op.description:
                out += [op.description, ""]
            if op.auth:
                out += [
                    '!!! info "Requires credentials"',
                    "    See [Configuration](../configuration.md).",
                    "",
                ]
            out += ["```bash", usage_line(op), "```", ""]
            params = op.parameters()
            if params:
                out += [
                    "| Parameter | Type | Required | Default | Description |",
                    "|---|---|---|---|---|",
                ]
                for p in params:
                    default = (
                        "" if p["required"] or p["default"] in (None, []) else f"`{p['default']}`"
                    )
                    ptype = p["type"].replace("|", "\\|")
                    out.append(
                        f"| `{p['name']}` | `{ptype}` | {'yes' if p['required'] else ''} | {default} | {p['description']} |"
                    )
                out.append("")
            if op.examples:
                out += ["Examples:", "", "```bash", *op.examples, "```", ""]
    return "\n".join(out)


MARKERS = {
    "<!-- secsdk:providers -->": providers_table,
    "<!-- secsdk:settings -->": settings_tables,
    "<!-- secsdk:operations -->": operations_reference,
}


def on_page_markdown(markdown: str, **kwargs: Any) -> str:
    for marker, render in MARKERS.items():
        if marker in markdown:
            markdown = markdown.replace(marker, render())
    return markdown
