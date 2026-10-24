"""Configuration.

All settings come from environment variables (or a ``.env`` file). Core settings
use the ``SECSDK_`` prefix; each provider defines its own settings class with a
``SECSDK_<PROVIDER>_`` prefix, and may also accept well-known vendor variable
names (e.g. ``AZURE_TENANT_ID``).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "secsdk"


class ProviderSettings(BaseSettings):
    """Base class for provider settings. Subclasses set ``env_prefix``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)


class CoreSettings(BaseSettings):
    """Settings shared by every provider."""

    model_config = SettingsConfigDict(env_prefix="SECSDK_", env_file=".env", extra="ignore")

    timeout: float = Field(30.0, description="Default HTTP timeout in seconds.")
    max_retries: int = Field(3, description="Retries for 429/5xx and network errors.")
    cache_enabled: bool = Field(
        True, description="Cache responses from public, slow-changing sources."
    )
    cache_dir: Path = Field(DEFAULT_CACHE_DIR, description="Where cached responses are stored.")
    rate_limit: bool = Field(True, description="Respect per-provider client-side rate limits.")
    user_agent: str = Field(
        "secsdk/0.1 (+https://github.com/Lariat-Labs/security-tools-sdk)",
        description="User-Agent sent to upstream APIs (SEC has its own setting).",
    )
