"""secctl: the command-line face of secsdk.

Built-in commands live here; one sub-command group per SDK provider is generated
at start-up from the registry (see :mod:`secctl.dynamic`).

Conventions (stable, agent-friendly):

* stdout carries one JSON envelope: ``{"ok", "operation", "data" | "error", "meta"}``.
* Exit codes: 0 ok, 1 internal, 2 invalid input / unknown op, 3 not configured or
  auth failed, 4 upstream error, 5 not found.
* ``--format table`` is for humans; ``--format jsonl`` streams list items.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import click

from secsdk import Client, CoreSettings, SecSDKError, default_registry
from secsdk import __version__ as sdk_version
from secsdk.result import Result
from secsdk.tools import tool_definitions

from . import __version__
from .dynamic import build_provider_group, command_name, usage_line
from .output import FORMATS, emit, emit_value

EXIT_CODES = {
    0: "success",
    1: "internal error",
    2: "invalid input or unknown operation",
    3: "provider not configured, or credentials rejected",
    4: "upstream API error or rate limit",
    5: "not found",
}


# ----- shared options ---------------------------------------------------------------------


def output_options(f: Any) -> Any:
    f = click.option(
        "-f",
        "--format",
        "fmt",
        type=click.Choice(FORMATS),
        default=None,
        help="Output format (default: json, or $SECCTL_FORMAT).",
    )(f)
    f = click.option(
        "--fields", default=None, help="Comma-separated keys to keep in each result row."
    )(f)
    f = click.option("--compact", is_flag=True, default=False, help="Single-line JSON.")(f)
    return f


def _out(ctx: click.Context, fmt: str | None, fields: str | None, compact: bool) -> dict[str, Any]:
    root = ctx.find_root().obj or {}
    return {
        "fmt": fmt or root.get("fmt") or os.environ.get("SECCTL_FORMAT", "json"),
        "fields": [f.strip() for f in fields.split(",") if f.strip()] if fields else None,
        "compact": compact or root.get("compact", False),
    }


def _client(ctx: click.Context) -> Client:
    root = ctx.find_root()
    obj = root.obj
    if obj.get("client") is None:
        overrides: dict[str, Any] = {}
        if obj.get("no_cache"):
            overrides["cache_enabled"] = False
        obj["client"] = Client(CoreSettings(**overrides))
        root.call_on_close(obj["client"].close)
    return obj["client"]


def run_operation(
    ctx: click.Context,
    fqname: str,
    params: dict[str, Any],
    fmt: str | None,
    fields: str | None,
    compact: bool,
) -> None:
    result = _client(ctx).execute(fqname, params)
    ctx.exit(emit(result, **_out(ctx, fmt, fields, compact)))


def _emit_local(
    ctx: click.Context, name: str, data: Any, fmt: str | None, fields: str | None, compact: bool
) -> None:
    ctx.exit(emit_value(name, data, **_out(ctx, fmt, fields, compact)))


def _emit_error(ctx: click.Context, name: str, exc: SecSDKError, fmt: str | None = None) -> None:
    result = Result(ok=False, operation=name, error=exc.to_dict() | {"exit_code": exc.exit_code})
    ctx.exit(emit(result, **_out(ctx, fmt, None, False)))


def load_env_file(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; existing environment wins."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


# ----- root group ---------------------------------------------------------------------------


class SecctlGroup(click.Group):
    """Lists built-in commands and provider groups in separate help sections."""

    BUILTINS = ("guide", "providers", "ops", "describe", "schema", "call", "doctor", "cache", "dev")

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        builtins: list[tuple[str, str]] = []
        providers: list[tuple[str, str]] = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            row = (name, cmd.get_short_help_str(80))
            (builtins if name in self.BUILTINS else providers).append(row)
        if builtins:
            with formatter.section("Commands"):
                formatter.write_dl(builtins)
        if providers:
            with formatter.section("Providers (secctl <provider> --help)"):
                formatter.write_dl(providers)


@click.group(
    cls=SecctlGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 110},
)
@click.version_option(f"{__version__} (secsdk {sdk_version})", prog_name="secctl")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(FORMATS),
    default=None,
    help="Default output format for all commands.",
)
@click.option("--compact", is_flag=True, help="Single-line JSON output.")
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Load KEY=VALUE settings from a file.",
)
@click.option("--no-cache", is_flag=True, help="Bypass the response cache for this run.")
@click.pass_context
def cli(
    ctx: click.Context, fmt: str | None, compact: bool, env_file: Path | None, no_cache: bool
) -> None:
    """Security data from many sources through one interface.

    Every SDK operation is a command: `secctl <provider> <operation> [args]`.
    Output is a JSON envelope by default. Start with `secctl guide`.
    """
    if env_file:
        load_env_file(env_file)
    ctx.obj = {"fmt": fmt, "compact": compact, "no_cache": no_cache, "client": None}


# ----- discovery commands ---------------------------------------------------------------------


@cli.command()
def guide() -> None:
    """Markdown cheat-sheet for humans and agents: conventions + every operation."""
    reg = default_registry()
    lines = [
        "# secctl guide",
        "",
        "Run `secctl <provider> <operation> [args] [options]`. Output is one JSON object:",
        '`{"ok": true, "operation": ..., "data": ..., "meta": ...}` or `{"ok": false, "error": {"code", "message", "hint"}}`.',
        "",
        "- Details for one operation: `secctl describe <provider>.<operation>` (parameters, JSON schema, examples).",
        '- Generic form: `secctl call <provider>.<operation> --params \'{"key": "value"}\'`.',
        "- Trim output with `--fields a,b`; humans can use `-f table`.",
        "- Check which providers have credentials: `secctl providers`.",
        "",
        "Exit codes: " + "; ".join(f"{k} {v}" for k, v in EXIT_CODES.items()) + ".",
        "",
    ]
    for cls in reg.providers():
        lines.append(f"## {cls.name}: {cls.title}")
        lines.append(cls.description)
        lines.append("")
        for op in sorted(cls.operations.values(), key=lambda o: o.name):
            lock = " (needs credentials)" if op.auth else ""
            lines.append(f"- `{usage_line(op)}`: {op.summary}{lock}")
        lines.append("")
    click.echo("\n".join(lines))


@cli.command()
@output_options
@click.pass_context
def providers(ctx: click.Context, fmt: str | None, fields: str | None, compact: bool) -> None:
    """Providers, their operations and whether credentials are configured."""
    client = _client(ctx)
    rows = []
    for cls in client.registry.providers():
        status = client.provider(cls.name).auth_status()
        rows.append(
            {
                "provider": cls.name,
                "title": cls.title,
                "operations": len(cls.operations),
                "auth_required": status.required,
                "configured": status.configured,
                "missing": status.missing,
                "note": status.note,
            }
        )
    _emit_local(ctx, "secctl.providers", rows, fmt, fields, compact)


@cli.command()
@click.argument("provider", required=False)
@output_options
@click.pass_context
def ops(
    ctx: click.Context, provider: str | None, fmt: str | None, fields: str | None, compact: bool
) -> None:
    """List operations (optionally for one provider)."""
    reg = default_registry()
    try:
        selected = [reg.get(provider)] if provider else reg.providers()
    except SecSDKError as exc:
        _emit_error(ctx, "secctl.ops", exc, fmt)
        return
    rows = [
        {
            "operation": op.fqname,
            "summary": op.summary,
            "requires_auth": op.auth,
            "usage": usage_line(op),
        }
        for cls in selected
        for op in sorted(cls.operations.values(), key=lambda o: o.name)
    ]
    _emit_local(ctx, "secctl.ops", rows, fmt, fields, compact)


@cli.command()
@click.argument("operation", nargs=-1, required=True)
@output_options
@click.pass_context
def describe(
    ctx: click.Context,
    operation: tuple[str, ...],
    fmt: str | None,
    fields: str | None,
    compact: bool,
) -> None:
    """Parameters, JSON schema and examples for one operation (`nvd.cve` or `nvd cve`)."""
    name = ".".join(operation) if len(operation) == 2 else operation[0]
    name = name.replace("-", "_")
    try:
        op = default_registry().operation(name)
    except SecSDKError as exc:
        _emit_error(ctx, "secctl.describe", exc, fmt)
        return
    info = op.describe() | {
        "usage": usage_line(op),
        "command": f"secctl {op.provider} {command_name(op)}",
    }
    out_schema = op.output_schema()
    if out_schema:
        info["output_schema"] = out_schema
    _emit_local(ctx, "secctl.describe", info, fmt, fields, compact)


@cli.command()
@click.option(
    "--style",
    type=click.Choice(["anthropic", "openai", "plain"]),
    default="anthropic",
    show_default=True,
)
@click.option("--provider", "providers_", multiple=True, help="Only these providers (repeatable).")
@click.pass_context
def schema(ctx: click.Context, style: str, providers_: tuple[str, ...]) -> None:
    """Tool definitions (JSON) for every operation, for wiring into an LLM tool loop."""
    tools = tool_definitions(style=style, providers=list(providers_) or None)  # type: ignore[arg-type]
    click.echo(json.dumps(tools, indent=2))


@cli.command()
@click.argument("operation")
@click.option("--params", "params_json", default=None, help="Parameters as a JSON object.")
@click.option(
    "-p",
    "--param",
    "pairs",
    multiple=True,
    help="key=value (value parsed as JSON when possible). Repeatable.",
)
@output_options
@click.pass_context
def call(
    ctx: click.Context,
    operation: str,
    params_json: str | None,
    pairs: tuple[str, ...],
    fmt: str | None,
    fields: str | None,
    compact: bool,
) -> None:
    """Run any operation by name with JSON parameters.

    \b
    secctl call kev.lookup -p cve_id=CVE-2023-4966
    secctl call epss.score --params '{"cve_ids": ["CVE-2021-44228"]}'
    """
    params: dict[str, Any] = {}
    if params_json:
        try:
            loaded = json.loads(params_json)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"not valid JSON: {exc}", param_hint="--params") from exc
        if not isinstance(loaded, dict):
            raise click.BadParameter("must be a JSON object", param_hint="--params")
        params.update(loaded)
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise click.BadParameter(f"expected key=value, got {pair!r}", param_hint="-p")
        try:
            params[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            params[key.strip()] = raw
    run_operation(ctx, operation.replace("-", "_"), params, fmt, fields, compact)


@cli.command()
@output_options
@click.pass_context
def doctor(ctx: click.Context, fmt: str | None, fields: str | None, compact: bool) -> None:
    """Environment check: versions, cache, and provider configuration."""
    client = _client(ctx)
    s = client.settings
    data = {
        "secctl": __version__,
        "secsdk": sdk_version,
        "python": sys.version.split()[0],
        "cache": {"enabled": s.cache_enabled, "dir": str(s.cache_dir)},
        "rate_limit": s.rate_limit,
        "providers": {
            cls.name: client.provider(cls.name).auth_status().model_dump(exclude_none=True)
            for cls in client.registry.providers()
        },
    }
    _emit_local(ctx, "secctl.doctor", data, fmt, fields, compact)


@cli.group()
def cache() -> None:
    """Manage the response cache."""


@cache.command("clear")
@click.pass_context
def cache_clear(ctx: click.Context) -> None:
    """Delete all cached responses."""
    removed = _client(ctx).http.cache.clear()
    _emit_local(ctx, "secctl.cache.clear", {"removed": removed}, None, None, False)


@cli.group()
def dev() -> None:
    """Developer helpers for growing the SDK."""


@dev.command("scaffold")
@click.argument("name")
@click.option("--auth", is_flag=True, help="Generate an API-key settings class and auth check.")
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@click.pass_context
def dev_scaffold(ctx: click.Context, name: str, auth: bool, force: bool) -> None:
    """Create a new provider module + test in the SDK source tree."""
    from .scaffold import scaffold

    try:
        written = scaffold(name, auth=auth, force=force)
    except (ValueError, FileExistsError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_local(
        ctx, "secctl.dev.scaffold", {"written": [str(p) for p in written]}, None, None, False
    )


# ----- provider groups ------------------------------------------------------------------------


def _register_provider_groups() -> None:
    for cls in default_registry().providers():
        if cls.name in cli.commands:
            continue
        cli.add_command(build_provider_group(cls, output_options, run_operation))


_register_provider_groups()


def main() -> None:
    cli(prog_name="secctl")


if __name__ == "__main__":
    main()
