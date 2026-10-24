"""Turn SDK operations into click commands.

Nothing here knows about specific providers. Every ``@operation`` in the SDK
becomes ``secctl <provider> <operation>`` automatically:

* required parameters become positional arguments (lists take several values),
* optional parameters become ``--options`` (lists are repeatable or comma-separated),
* booleans become ``--flag/--no-flag``,
* values are passed to the SDK untouched, which validates them with pydantic.
"""

from __future__ import annotations

import enum
import json
import types
import typing
from typing import Any

import click
from pydantic.fields import FieldInfo

from secsdk.operation import Operation
from secsdk.provider import Provider


def unwrap(annotation: Any) -> tuple[Any, bool]:
    """Return (base type, is_list), stripping Optional/Annotated."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return unwrap(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(members) == 1:
            return unwrap(members[0])
        return annotation, False
    if origin in (list, set, tuple, frozenset):
        args = typing.get_args(annotation)
        inner, _ = unwrap(args[0]) if args else (str, False)
        return inner, True
    return annotation, False


def choices_of(base: Any) -> list[str] | None:
    if typing.get_origin(base) is typing.Literal:
        return [str(a) for a in typing.get_args(base)]
    if isinstance(base, type) and issubclass(base, enum.Enum):
        return [str(m.value) for m in base]
    return None


def click_type(base: Any) -> click.ParamType:
    choices = choices_of(base)
    if choices:
        return click.Choice(choices, case_sensitive=False)
    if base is int:
        return click.INT
    if base is float:
        return click.FLOAT
    return click.STRING


def is_complex(base: Any) -> bool:
    return (
        isinstance(base, type)
        and base not in (str, int, float, bool)
        and not choices_of(base)
        and not issubclass(base, enum.Enum)
    )


def build_params(op: Operation) -> list[click.Parameter]:
    params: list[click.Parameter] = []
    for name, field in op.input_model.model_fields.items():
        params.append(_param(name, field))
    return params


def _param(name: str, field: FieldInfo) -> click.Parameter:
    base, is_list = unwrap(field.annotation)
    flag = "--" + name.replace("_", "-")
    desc = field.description or ""
    choices = choices_of(base)

    if field.is_required():
        if base is bool and not is_list:
            return click.Option([f"{flag}/--no-{name.replace('_', '-')}"], required=True, help=desc)
        if is_list:
            return click.Argument([name], nargs=-1, required=True, type=click.STRING)
        return click.Argument([name], type=click_type(base))

    default = field.get_default(call_default_factory=True)
    if base is bool and not is_list:
        return click.Option(
            [f"{flag}/--no-{name.replace('_', '-')}"],
            default=bool(default),
            show_default=True,
            help=desc,
        )
    if is_list:
        extra = f" One of: {', '.join(choices)}." if choices else ""
        return click.Option(
            [flag],
            multiple=True,
            type=click.STRING,
            help=f"{desc}{extra} Repeat or comma-separate. Default: {', '.join(map(str, default)) or 'none'}.".strip(),
        )
    if is_complex(base):
        return click.Option([flag], type=click.STRING, help=f"{desc} (JSON)".strip())
    return click.Option(
        [flag], type=click_type(base), default=default, show_default=default is not None, help=desc
    )


def collect(ctx: click.Context, op: Operation, values: dict[str, Any]) -> dict[str, Any]:
    """Keep only values the user actually supplied so SDK defaults stay authoritative."""
    out: dict[str, Any] = {}
    for name, field in op.input_model.model_fields.items():
        source = ctx.get_parameter_source(name)
        if source in (click.core.ParameterSource.DEFAULT, None):
            continue
        value = values.get(name)
        base, is_list = unwrap(field.annotation)
        if is_list:
            items: list[str] = []
            for v in value or ():
                items.extend(p.strip() for p in str(v).split(",") if p.strip())
            value = items
        elif is_complex(base) and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise click.BadParameter(f"expected JSON: {exc}", param_hint=f"--{name}") from exc
        out[name] = value
    return out


def command_name(op: Operation) -> str:
    return op.name.replace("_", "-")


def usage_line(op: Operation) -> str:
    parts = ["secctl", op.provider, command_name(op)]
    for p in op.parameters():
        if p["required"]:
            parts.append(f"<{p['name']}>" + ("..." if p["type"].startswith("list") else ""))
    if any(not p["required"] for p in op.parameters()):
        parts.append("[options]")
    return " ".join(parts)


def build_op_command(op: Operation, output_options: Any, run: Any) -> click.Command:
    help_text = op.summary + ("\n\n" + op.description if op.description else "")
    if op.auth:
        help_text += "\n\nRequires credentials: see `secctl providers`."
    epilog = None
    if op.examples:
        epilog = "Examples:\n\n" + "\n\n".join(f"  {e}" for e in op.examples)

    @click.pass_context
    def callback(ctx: click.Context, **kwargs: Any) -> None:
        out_opts = {k: kwargs.pop(k) for k in ("fmt", "fields", "compact")}
        params = collect(ctx, op, kwargs)
        run(ctx, op.fqname, params, **out_opts)

    cmd = click.Command(
        name=command_name(op),
        callback=callback,
        params=build_params(op),
        help=help_text,
        short_help=op.summary,
        epilog=epilog,
        context_settings={"max_content_width": 110},
    )
    return output_options(cmd)


def build_provider_group(cls: type[Provider], output_options: Any, run: Any) -> click.Group:
    help_text = f"{cls.title or cls.name}: {cls.description}"
    if cls.docs_url:
        help_text += f"\n\nUpstream docs: {cls.docs_url}"
    group = click.Group(name=cls.name, help=help_text, short_help=cls.description)
    for op in sorted(cls.operations.values(), key=lambda o: o.name):
        group.add_command(build_op_command(op, output_options, run))
    return group
