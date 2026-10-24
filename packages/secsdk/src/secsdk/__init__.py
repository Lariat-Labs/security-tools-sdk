"""secsdk: a structured, provider-based SDK for security data sources."""

__version__ = "0.1.0"

from .client import Client
from .config import CoreSettings, ProviderSettings
from .errors import (
    AuthError,
    ConfigError,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
    SecSDKError,
    UnknownOperationError,
    UpstreamError,
)
from .operation import Operation, operation
from .provider import AuthStatus, Provider
from .registry import Registry, default_registry
from .result import Result

__all__ = [
    "AuthError",
    "AuthStatus",
    "Client",
    "ConfigError",
    "CoreSettings",
    "InvalidInputError",
    "NotFoundError",
    "Operation",
    "Provider",
    "ProviderSettings",
    "RateLimitedError",
    "Registry",
    "Result",
    "SecSDKError",
    "UnknownOperationError",
    "UpstreamError",
    "__version__",
    "default_registry",
    "operation",
]
