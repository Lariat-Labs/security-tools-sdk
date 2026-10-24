"""The ``@operation`` decorator and the :class:`Operation` descriptor.

An *operation* is a provider method that has been declared as part of the public
surface. Declaring it does three things:

1. Builds a pydantic input model from the method signature, so every call (from
   Python, the CLI, or an agent) is validated the same way.
2. Records metadata (summary, auth requirement, examples) used by the CLI,
   the generated docs and the agent tool schemas.
3. Makes the operation discoverable by name: ``"<provider>.<operation>"``.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, create_model

OP_ATTR = "__secsdk_operation__"


@dataclass(frozen=True)
class OperationSpec:
    """What the decorator records before the provider class is finished."""

    name: str | None = None
    auth: bool = False
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()


@dataclass
class Operation:
    """A fully-resolved operation, attached to a provider class."""

    provider: str
    name: str
    method: str
    func: Callable[..., Any]
    input_model: type[BaseModel]
    return_type: Any
    summary: str
    description: str
    auth: bool = False
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fqname(self) -> str:
        return f"{self.provider}.{self.name}"

    @property
    def tool_name(self) -> str:
        """Name safe for LLM tool APIs (no dots)."""
        return f"{self.provider}_{self.name}"

    def input_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return schema

    def output_schema(self) -> dict[str, Any] | None:
        if self.return_type in (inspect.Signature.empty, Any, None):
            return None
        try:
            return TypeAdapter(self.return_type).json_schema()
        except Exception:
            return None

    def parameters(self) -> list[dict[str, Any]]:
        """Flat parameter description (used by CLI help and docs)."""
        out = []
        for pname, finfo in self.input_model.model_fields.items():
            out.append(
                {
                    "name": pname,
                    "type": _type_label(finfo.annotation),
                    "required": finfo.is_required(),
                    "default": None
                    if finfo.is_required()
                    else finfo.get_default(call_default_factory=True),
                    "description": finfo.description or "",
                }
            )
        return out

    def describe(self) -> dict[str, Any]:
        return {
            "operation": self.fqname,
            "summary": self.summary,
            "description": self.description,
            "requires_auth": self.auth,
            "tags": list(self.tags),
            "parameters": self.parameters(),
            "examples": list(self.examples),
            "input_schema": self.input_schema(),
        }


def operation(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    auth: bool = False,
    tags: Sequence[str] = (),
    examples: Sequence[str] = (),
) -> Any:
    """Mark a provider method as a public operation.

    Use bare (``@operation``) or with options::

        @operation(auth=True, examples=["secctl tenable scans"])
        def scans(self) -> list[Scan]:
            \"\"\"List scans.\"\"\"
    """

    def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
        setattr(
            f,
            OP_ATTR,
            OperationSpec(name=name, auth=auth, tags=tuple(tags), examples=tuple(examples)),
        )
        return f

    return wrap(func) if func is not None else wrap


def build_operation(
    provider_name: str, cls_name: str, attr: str, func: Callable[..., Any]
) -> Operation:
    spec: OperationSpec = getattr(func, OP_ATTR)
    hints = typing.get_type_hints(func, include_extras=True)
    sig = inspect.signature(func)
    fields: dict[str, Any] = {}
    for pname, param in list(sig.parameters.items())[1:]:  # skip self
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise TypeError(f"{cls_name}.{attr}: *args/**kwargs are not allowed in operations")
        annotation = hints.get(pname, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[pname] = (annotation, default)

    model = create_model(  # type: ignore[call-overload]
        f"{cls_name}{_camel(attr)}Input",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    doc = inspect.getdoc(func) or ""
    summary, _, rest = doc.partition("\n")
    return Operation(
        provider=provider_name,
        name=spec.name or attr,
        method=attr,
        func=func,
        input_model=model,
        return_type=hints.get("return", inspect.Signature.empty),
        summary=summary.strip(),
        description=rest.strip(),
        auth=spec.auth,
        tags=spec.tags,
        examples=spec.examples,
    )


def _camel(s: str) -> str:
    return "".join(p.capitalize() for p in s.split("_"))


def _type_label(annotation: Any) -> str:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Literal:
        return " | ".join(repr(a) for a in args)
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        parts = [_type_label(a) for a in args if a is not type(None)]
        return " | ".join(parts)
    if origin in (list, tuple, set, Sequence):
        return f"list[{_type_label(args[0])}]" if args else "list"
    if origin is typing.Annotated:
        return _type_label(args[0])
    return getattr(annotation, "__name__", str(annotation))
