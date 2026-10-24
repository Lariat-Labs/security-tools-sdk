"""Validate the user's token for this app, and exchange it for a gateway token (OBO).

The exchange authenticates this app with a **federated credential from its managed
identity**, not a client secret: the managed identity's token for
``api://AzureADTokenExchange`` is the client assertion. For local development a
client secret (``AGW_CHAT_CLIENT_SECRET``) also works.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import jwt

LOGIN = "https://login.microsoftonline.com"
EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class TokenValidator:
    """Checks a v2 access token issued to this app: signature, issuer, audience, scope."""

    def __init__(
        self,
        tenant_id: str,
        app_id: str,
        scope: str = "access_as_user",
        key_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.tenant_id, self.app_id, self.scope = tenant_id, app_id, scope
        if key_resolver is None:
            jwks = jwt.PyJWKClient(f"{LOGIN}/{tenant_id}/discovery/v2.0/keys", cache_keys=True)
            key_resolver = lambda token: jwks.get_signing_key_from_jwt(token).key  # noqa: E731
        self._key = key_resolver

    def validate(self, token: str) -> dict[str, Any]:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._key(token),
                algorithms=["RS256"],
                audience=[self.app_id, f"api://{self.app_id}"],
                issuer=f"{LOGIN}/{self.tenant_id}/v2.0",
                options={"require": ["exp", "iat", "aud", "iss", "oid"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(401, "invalid_token", f"Sign in again ({exc}).") from exc
        if self.scope not in str(claims.get("scp", "")).split():
            raise AuthError(403, "missing_scope", f"The token needs the {self.scope} scope.")
        return claims


def managed_identity_assertion(client_id: str | None) -> Callable[[], str]:
    """Client assertion from a managed identity (federated identity credential)."""
    from azure.identity import ManagedIdentityCredential

    credential = ManagedIdentityCredential(client_id=client_id)
    return lambda: credential.get_token(EXCHANGE_SCOPE).token


class OboTokenProvider:
    """Exchanges user tokens for gateway tokens, reusing one credential per user token.

    Each ``OnBehalfOfCredential`` caches the token it obtains, so repeated requests
    with the same user token don't go back to Entra.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        gateway_scope: str,
        *,
        client_assertion_func: Callable[[], str] | None = None,
        client_secret: str | None = None,
        credential_factory: Callable[..., Any] | None = None,
        max_cached: int = 256,
    ) -> None:
        if not client_assertion_func and not client_secret:
            raise ValueError("OBO needs a client assertion (managed identity) or a client secret")
        if credential_factory is None:
            from azure.identity import OnBehalfOfCredential

            credential_factory = OnBehalfOfCredential
        self.tenant_id, self.client_id, self.gateway_scope = tenant_id, client_id, gateway_scope
        self._assertion, self._secret = client_assertion_func, client_secret
        self._factory = credential_factory
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._max = max_cached
        self._lock = threading.Lock()

    def token_for(self, user_token: str) -> str:
        key = hashlib.sha256(user_token.encode()).hexdigest()
        with self._lock:
            credential = self._cache.get(key)
            if credential is None:
                kwargs: dict[str, Any] = {"user_assertion": user_token}
                if self._assertion:
                    kwargs["client_assertion_func"] = self._assertion
                else:
                    kwargs["client_secret"] = self._secret
                credential = self._factory(self.tenant_id, self.client_id, **kwargs)
                self._cache[key] = credential
                if len(self._cache) > self._max:
                    self._cache.popitem(last=False)
            else:
                self._cache.move_to_end(key)
        try:
            return str(credential.get_token(self.gateway_scope).token)
        except Exception as exc:  # azure-identity raises ClientAuthenticationError
            raise AuthError(
                502, "obo_failed", f"Couldn't get a gateway token for you: {exc}"
            ) from exc


def peek_claims(token: str) -> dict[str, Any]:
    """Claims of a token we received from Entra ourselves (display only, not trusted)."""
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return claims
