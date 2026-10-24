"""The :class:`Client`: the one object you need.

>>> from secsdk import Client
>>> with Client() as sec:
...     sec.kev.lookup("CVE-2021-44228")          # attribute style
...     sec.call("epss.score", cve_ids=["CVE-2021-44228"])  # by name
...     sec.execute("osint.dns", {"name": "example.com"})   # enveloped Result
"""

from __future__ import annotations

import time
from typing import Any, TypeVar, cast

import httpx

from . import __version__
from .config import CoreSettings, ProviderSettings
from .errors import SecSDKError, UnknownOperationError
from .http import HttpClient
from .operation import Operation
from .provider import Provider
from .registry import Registry, default_registry
from .result import Result

P = TypeVar("P", bound=Provider)


class Client:
    def __init__(
        self,
        settings: CoreSettings | None = None,
        *,
        registry: Registry | None = None,
        provider_settings: dict[str, ProviderSettings] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or CoreSettings()
        self.registry = registry or default_registry()
        self.http = HttpClient(self.settings, transport=transport)
        self._provider_settings = dict(provider_settings or {})
        self._instances: dict[str, Provider] = {}

    # ----- context manager ----------------------------------------------------------

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.http.close()

    # ----- providers ----------------------------------------------------------------

    def provider(self, name: str) -> Provider:
        inst = self._instances.get(name)
        if inst is None:
            cls = self.registry.get(name)
            inst = cls(self, self._provider_settings.get(name))
            self._instances[name] = inst
        return inst

    def get(self, cls: type[P]) -> P:
        """Typed access: ``client.get(KEV).lookup(...)`` type-checks fully."""
        return cast(P, self.provider(cls.name))

    def __getattr__(self, name: str) -> Provider:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self.provider(name)
        except UnknownOperationError:
            raise AttributeError(f"Client has no provider '{name}'") from None

    def operations(self) -> list[Operation]:
        return self.registry.operations()

    # ----- calling ------------------------------------------------------------------

    def call(self, fqname: str, /, **params: Any) -> Any:
        """Run ``provider.operation`` and return its data. Raises :class:`SecSDKError`."""
        op = self.registry.operation(fqname)
        return self.provider(op.provider).invoke(op.name, params)

    def execute(self, fqname: str, params: dict[str, Any] | None = None) -> Result:
        """Run an operation and always return a :class:`Result` (never raises)."""
        start = time.perf_counter()
        meta: dict[str, Any] = {"sdk_version": __version__}
        try:
            data = self.call(fqname, **(params or {}))
            ok, error = True, None
            if isinstance(data, list):
                meta["count"] = len(data)
        except SecSDKError as exc:
            data, ok = None, False
            error = exc.to_dict() | {"exit_code": exc.exit_code}
        except Exception as exc:  # last-resort guard so callers always get an envelope
            data, ok = None, False
            error = {
                "code": "internal_error",
                "type": type(exc).__name__,
                "message": str(exc),
                "exit_code": 1,
            }
        meta["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
        return Result(ok=ok, operation=fqname, data=data, error=error, meta=meta)
