"""The credential the runtime uses for model calls.

In Azure it is ``DefaultAzureCredential`` (the runtime's managed identity). For a local
container, ``run_local.py`` passes ``AGW_DEV_ACCESS_TOKEN``: a short-lived token from
``az account get-access-token`` for the model endpoint only, so the container never
holds a refresh token or an Azure CLI sign-in. A dev token is refused in Container Apps.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

DEV_TOKEN_ENV = "AGW_DEV_ACCESS_TOKEN"


def token_claims(token: str) -> dict[str, Any]:
    """The claims of a JWT, decoded without verification (only to read exp and aud)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, binascii.Error) as exc:
        raise ValueError(f"{DEV_TOKEN_ENV} is not a JWT access token") from exc
    if not isinstance(claims, dict) or not isinstance(claims.get("exp"), int):
        raise ValueError(f"{DEV_TOKEN_ENV} has no expiry (exp) claim")
    return claims


def _audience_matches(aud: Any, scope: str) -> bool:
    resource = scope.removesuffix("/.default").rstrip("/")
    return isinstance(aud, str) and resource in (aud.rstrip("/"), f"api://{aud}")


class DevTokenCredential:
    """A fixed access token, valid until it expires; there is no refresh."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.expires_on: int = token_claims(token)["exp"]

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        from azure.core.credentials import AccessToken
        from azure.core.exceptions import ClientAuthenticationError

        if time.time() >= self.expires_on - 30:
            expired = datetime.fromtimestamp(self.expires_on, UTC).strftime("%H:%M UTC")
            raise ClientAuthenticationError(
                f"The dev access token expired at {expired}. "
                "Restart the container with deploy/agent-gateway/run_local.py for a new one."
            )
        return AccessToken(self.token, self.expires_on)


def credential_from_env(scope: str, env: Mapping[str, str] | None = None) -> Any:
    """``DevTokenCredential`` when ``AGW_DEV_ACCESS_TOKEN`` is set, else ``DefaultAzureCredential``.

    Raises ``RuntimeError`` for a dev token in Container Apps, and ``ValueError`` for a
    token that isn't for ``scope`` or has already expired, so a misconfigured container
    fails at start-up rather than on the first run.
    """
    env = os.environ if env is None else env
    token = env.get(DEV_TOKEN_ENV, "").strip()
    if not token:
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential()
    if env.get("CONTAINER_APP_NAME"):
        raise RuntimeError(f"{DEV_TOKEN_ENV} is for local containers only; unset it in Azure.")
    claims = token_claims(token)
    if not _audience_matches(claims.get("aud"), scope):
        raise ValueError(
            f"{DEV_TOKEN_ENV} is for {claims.get('aud')!r}, but AGW_GATEWAY_SCOPE is {scope!r}."
        )
    credential = DevTokenCredential(token)
    if time.time() >= credential.expires_on - 30:
        raise ValueError(f"{DEV_TOKEN_ENV} has expired; get a new one.")
    return credential
