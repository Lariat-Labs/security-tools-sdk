"""The :class:`Provider` base class.

A provider wraps one upstream source (NVD, crt.sh, Tenable, Azure...). Subclass
it, set ``name``, and decorate public methods with ``@operation``. Drop the
module into ``secsdk/providers/`` (or publish it under the ``secsdk.providers``
entry-point group) and it appears in the SDK, the CLI, the docs and the agent
tool list automatically.
"""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ValidationError

from . import telemetry
from .config import ProviderSettings
from .errors import (
    ConfigError,
    InvalidInputError,
    SecSDKError,
    UnknownOperationError,
    UpstreamError,
)
from .operation import OP_ATTR, Operation, build_operation
from .ratelimit import RateLimiter

if TYPE_CHECKING:
    from .client import Client


class AuthStatus(BaseModel):
    required: bool
    configured: bool
    missing: list[str] = []
    note: str | None = None


class Provider:
    #: Short, unique, lowercase id used in operation names (``nvd.cve``).
    name: ClassVar[str] = ""
    #: Human-friendly title.
    title: ClassVar[str] = ""
    #: One-paragraph description of the source.
    description: ClassVar[str] = ""
    #: Link to upstream API docs.
    docs_url: ClassVar[str | None] = None
    #: Settings class (env-driven). ``None`` means no configuration.
    settings_class: ClassVar[type[ProviderSettings] | None] = None
    #: ``(calls, seconds)`` client-side rate limit, or ``None``.
    rate_limit: ClassVar[tuple[int, float] | None] = None
    #: Filled in automatically from ``@operation`` methods.
    operations: ClassVar[dict[str, Operation]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        ops: dict[str, Operation] = {}
        for base in reversed(cls.__mro__[1:]):
            ops.update(getattr(base, "operations", {}) or {})
        for attr, value in list(cls.__dict__.items()):
            if callable(value) and hasattr(value, OP_ATTR):
                op = build_operation(cls.name or cls.__name__.lower(), cls.__name__, attr, value)
                ops[op.name] = op
                setattr(cls, attr, _bind_wrapper(op))
        # re-home inherited operations under this provider's name
        cls.operations = {
            k: (v if v.provider == cls.name else _rehome(v, cls.name)) for k, v in ops.items()
        }

    def __init__(self, client: Client, settings: ProviderSettings | None = None) -> None:
        self.client = client
        self.http = client.http
        if settings is None and self.settings_class is not None:
            settings = self.settings_class()
        self.settings = settings
        limits = self.limits()
        self.limiter = RateLimiter(*limits) if limits else None
        self.setup()

    # ----- hooks for subclasses -------------------------------------------------

    def setup(self) -> None:
        """Called at the end of ``__init__``."""

    def limits(self) -> tuple[int, float] | None:
        """Rate limit to apply. Override when it depends on settings (e.g. API key)."""
        return self.rate_limit

    def auth_status(self) -> AuthStatus:
        """Report whether credentials are configured. Override in credentialed providers."""
        needs = any(op.auth for op in self.operations.values())
        return AuthStatus(required=needs, configured=not needs)

    # ----- machinery --------------------------------------------------------------

    def ensure_configured(self) -> None:
        status = self.auth_status()
        if not status.configured:
            missing = ", ".join(status.missing) or "credentials"
            raise ConfigError(
                f"Provider '{self.name}' is not configured (missing: {missing})",
                hint=status.note or f"Set {missing} in the environment or a .env file.",
            )

    def invoke(self, op_name: str, params: dict[str, Any] | None = None) -> Any:
        op = self.operations.get(op_name)
        if op is None:
            raise UnknownOperationError(
                f"Provider '{self.name}' has no operation '{op_name}'",
                hint=f"Available: {', '.join(sorted(self.operations))}",
            )
        return _run(self, op, params or {})


def _run(provider: Provider, op: Operation, params: dict[str, Any]) -> Any:
    with telemetry.operation_span(op.fqname, op.provider, params) as span:
        try:
            result = _run_inner(provider, op, params)
        except SecSDKError as exc:
            span.fail(exc.code, exc)
            raise
        except Exception as exc:
            span.fail("internal_error", exc)
            raise
        if isinstance(result, list):
            span.set("secsdk.result.count", len(result))
        return result


def _run_inner(provider: Provider, op: Operation, params: dict[str, Any]) -> Any:
    try:
        validated = op.input_model.model_validate(params)
    except ValidationError as exc:
        raise InvalidInputError(
            f"Invalid parameters for {op.fqname}",
            details=[
                {"param": ".".join(str(p) for p in e["loc"]), "error": e["msg"]}
                for e in exc.errors()
            ],
        ) from exc
    if op.auth:
        provider.ensure_configured()
    kwargs = {k: getattr(validated, k) for k in op.input_model.model_fields}
    try:
        return op.func(provider, **kwargs)
    except ValidationError as exc:
        # A model built from an upstream response did not match: that's the upstream's fault.
        raise UpstreamError(
            f"{op.fqname}: unexpected response shape from upstream", details=str(exc)[:500]
        ) from exc


def _bind_wrapper(op: Operation) -> Any:
    sig = inspect.signature(op.func)

    @functools.wraps(op.func)
    def method(self: Provider, *args: Any, **kwargs: Any) -> Any:
        try:
            bound = sig.bind(self, *args, **kwargs)
        except TypeError as exc:
            raise InvalidInputError(f"{op.fqname}: {exc}") from exc
        params = dict(bound.arguments)
        params.pop(next(iter(sig.parameters)))
        return _run(self, self.operations[op.name], params)

    return method


def _rehome(op: Operation, provider: str) -> Operation:
    from dataclasses import replace

    return replace(op, provider=provider)
