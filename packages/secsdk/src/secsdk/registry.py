"""Provider discovery.

Providers are found in two places:

* every module inside the ``secsdk.providers`` package (built-ins), and
* any installed package that exposes a ``secsdk.providers`` entry point.

Adding a provider is therefore "add a file" or "pip install a plugin": nothing
else in the SDK, CLI or docs has to change.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import warnings
from functools import cache
from importlib.metadata import entry_points

from .errors import UnknownOperationError
from .operation import Operation
from .provider import Provider

ENTRY_POINT_GROUP = "secsdk.providers"


class Registry:
    def __init__(self) -> None:
        self._providers: dict[str, type[Provider]] = {}

    # ----- registration -------------------------------------------------------

    def register(self, cls: type[Provider]) -> type[Provider]:
        if not cls.name:
            raise ValueError(f"{cls.__name__} must set a non-empty `name`")
        existing = self._providers.get(cls.name)
        if existing is not None and existing is not cls:
            raise ValueError(f"Provider name '{cls.name}' is used by {existing} and {cls}")
        self._providers[cls.name] = cls
        return cls

    def load_builtins(self) -> None:
        from . import providers as pkg

        for mod_info in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            if mod_info.name.rsplit(".", 1)[-1].startswith("_"):
                continue
            module = importlib.import_module(mod_info.name)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, Provider)
                    and obj is not Provider
                    and obj.name
                    and obj.__module__ == module.__name__
                ):
                    self.register(obj)

    def load_entry_points(self) -> None:
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                obj = ep.load()
                self.register(obj)
            except Exception as exc:  # a broken plugin must not break the SDK
                warnings.warn(
                    f"secsdk: failed to load provider plugin {ep.name!r}: {exc}", stacklevel=2
                )

    # ----- lookup -----------------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self._providers)

    def providers(self) -> list[type[Provider]]:
        return [self._providers[n] for n in self.names()]

    def get(self, name: str) -> type[Provider]:
        try:
            return self._providers[name]
        except KeyError:
            raise UnknownOperationError(
                f"Unknown provider '{name}'", hint=f"Available providers: {', '.join(self.names())}"
            ) from None

    def operation(self, fqname: str) -> Operation:
        provider, _, op = fqname.partition(".")
        if not op:
            raise UnknownOperationError(
                f"'{fqname}' is not an operation name",
                hint="Use '<provider>.<operation>', e.g. 'nvd.cve'.",
            )
        cls = self.get(provider)
        try:
            return cls.operations[op]
        except KeyError:
            raise UnknownOperationError(
                f"Unknown operation '{fqname}'",
                hint=f"'{provider}' has: {', '.join(sorted(cls.operations))}",
            ) from None

    def operations(self) -> list[Operation]:
        return [
            op
            for cls in self.providers()
            for op in sorted(cls.operations.values(), key=lambda o: o.name)
        ]


@cache
def default_registry() -> Registry:
    reg = Registry()
    reg.load_builtins()
    reg.load_entry_points()
    return reg
