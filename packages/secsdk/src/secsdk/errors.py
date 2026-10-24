"""Exception hierarchy.

Every error carries a stable ``code`` and an ``exit_code`` so the CLI (and agents
reading its JSON) can react to the *kind* of failure without parsing messages.
"""

from __future__ import annotations

from typing import Any


class SecSDKError(Exception):
    """Base class for all SDK errors."""

    code: str = "error"
    exit_code: int = 1

    def __init__(self, message: str, *, hint: str | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "type": type(self).__name__,
            "message": self.message,
        }
        if self.hint:
            out["hint"] = self.hint
        if self.details is not None:
            out["details"] = self.details
        return out


class InvalidInputError(SecSDKError):
    """Parameters failed validation."""

    code = "invalid_input"
    exit_code = 2


class ConfigError(SecSDKError):
    """A provider needs credentials or settings that are not configured."""

    code = "not_configured"
    exit_code = 3


class AuthError(SecSDKError):
    """The upstream API rejected our credentials (401/403)."""

    code = "auth_failed"
    exit_code = 3


class UpstreamError(SecSDKError):
    """The upstream API failed or returned something we could not use."""

    code = "upstream_error"
    exit_code = 4

    def __init__(self, message: str, *, status: int | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        if self.status is not None:
            out["status"] = self.status
        return out


class RateLimitedError(UpstreamError):
    """The upstream API kept returning 429 after retries."""

    code = "rate_limited"


class NotFoundError(SecSDKError):
    """The requested object does not exist upstream."""

    code = "not_found"
    exit_code = 5


class UnknownOperationError(SecSDKError):
    """No provider/operation with that name is registered."""

    code = "unknown_operation"
    exit_code = 2
