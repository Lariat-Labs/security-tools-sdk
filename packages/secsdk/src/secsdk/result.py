"""The result envelope shared by every non-Python consumer (CLI, agent tools)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from pydantic_core import to_jsonable_python


class Result(BaseModel):
    """Uniform wrapper: ``ok`` + ``data`` on success, ``ok`` + ``error`` on failure."""

    ok: bool
    operation: str
    data: Any = None
    error: dict[str, Any] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        if self.ok:
            return 0
        return int((self.error or {}).get("exit_code", 1))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok, "operation": self.operation}
        if self.ok:
            out["data"] = to_jsonable(self.data)
        else:
            err = dict(self.error or {})
            err.pop("exit_code", None)
            out["error"] = err
        out["meta"] = self.meta
        return out


def to_jsonable(obj: Any) -> Any:
    """Convert models/dates/sets into plain JSON types (``None`` fields dropped from models)."""
    return to_jsonable_python(obj, exclude_none=True)
