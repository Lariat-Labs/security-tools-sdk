"""Tests for the web chat backend: token validation, OBO, gateway pass-through (offline)."""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

GATEWAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_DIR / "chat"))

from agw_chat.app import Settings, create_app  # noqa: E402
from agw_chat.auth import AuthError, OboTokenProvider, TokenValidator  # noqa: E402

TENANT = "11111111-1111-1111-1111-111111111111"
CHAT_APP = "22222222-2222-2222-2222-222222222222"
GATEWAY_APP = "33333333-3333-3333-3333-333333333333"
GATEWAY = "https://apim.example"
OID = "44444444-4444-4444-4444-444444444444"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def user_token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "aud": CHAT_APP,
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "oid": OID,
        "scp": "access_as_user",
        "azp": CHAT_APP,
        "name": "Test User",
    }
    claims.update(overrides)
    return jwt.encode(claims, KEY, algorithm="RS256")


def validator() -> TokenValidator:
    return TokenValidator(TENANT, CHAT_APP, key_resolver=lambda token: KEY.public_key())


# ----- validation -----------------------------------------------------------------------------


def test_valid_token():
    assert validator().validate(user_token())["oid"] == OID


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"aud": GATEWAY_APP}, "invalid_token"),  # token for another API
        ({"iss": "https://login.microsoftonline.com/other/v2.0"}, "invalid_token"),
        ({"exp": int(time.time()) - 10}, "invalid_token"),
        ({"scp": "User.Read"}, "missing_scope"),
    ],
)
def test_invalid_tokens(overrides, code):
    with pytest.raises(AuthError) as info:
        validator().validate(user_token(**overrides))
    assert info.value.code == code


def test_token_signed_by_another_key_is_rejected():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        jwt.decode(user_token(), options={"verify_signature": False}), other, "RS256"
    )
    with pytest.raises(AuthError):
        validator().validate(forged)


# ----- OBO ------------------------------------------------------------------------------------


class FakeObo:
    """Stands in for azure.identity.OnBehalfOfCredential."""

    created: list = []

    def __init__(self, tenant_id, client_id, **kwargs):
        self.kwargs = kwargs
        FakeObo.created.append(self)

    def get_token(self, scope):
        user = jwt.decode(self.kwargs["user_assertion"], options={"verify_signature": False})
        token = jwt.encode(
            {
                "aud": GATEWAY_APP,
                "oid": user["oid"],
                "scp": "Agents.Invoke",
                "azp": CHAT_APP,
                "groups": ["a", "b", "c"],
            },
            "x" * 32,
            algorithm="HS256",
        )
        return SimpleNamespace(token=token)


def obo() -> OboTokenProvider:
    FakeObo.created = []
    return OboTokenProvider(
        TENANT,
        CHAT_APP,
        f"api://{GATEWAY_APP}/.default",
        client_assertion_func=lambda: "mi-assertion",
        credential_factory=FakeObo,
    )


def test_obo_uses_the_managed_identity_assertion_and_reuses_credentials():
    provider = obo()
    t1, t2 = user_token(), user_token(name="other session")
    provider.token_for(t1)
    provider.token_for(t1)
    provider.token_for(t2)
    assert len(FakeObo.created) == 2  # one per distinct user token
    kwargs = FakeObo.created[0].kwargs
    assert kwargs["user_assertion"] == t1
    assert kwargs["client_assertion_func"]() == "mi-assertion"
    assert "client_secret" not in kwargs


def test_obo_needs_a_client_credential():
    with pytest.raises(ValueError):
        OboTokenProvider(TENANT, CHAT_APP, "scope")


# ----- HTTP API -------------------------------------------------------------------------------


def client():
    from fastapi.testclient import TestClient

    settings = Settings(
        tenant_id=TENANT, chat_app_id=CHAT_APP, gateway_url=GATEWAY, gateway_app_id=GATEWAY_APP
    )
    return TestClient(create_app(settings, validator(), obo(), httpx.Client()))


def auth(**overrides):
    return {"Authorization": f"Bearer {user_token(**overrides)}"}


def test_page_and_config_are_public():
    c = client()
    assert c.get("/").status_code == 200 and "msal-browser@" in c.get("/").text
    assert c.get("/api/config").json()["scope"] == f"api://{CHAT_APP}/access_as_user"
    assert c.get("/healthz").json() == {"ok": True}


def test_api_requires_a_valid_token():
    c = client()
    assert c.get("/api/agents").status_code == 401
    assert c.get("/api/agents", headers=auth(aud=GATEWAY_APP)).status_code == 401
    assert c.get("/api/agents", headers=auth(scp="other")).status_code == 403


def test_whoami_shows_both_tokens_for_the_same_user():
    body = client().get("/api/whoami", headers=auth()).json()
    assert body["token_for_chat_app"]["aud"] == CHAT_APP
    assert body["obo_token_for_gateway"]["aud"] == GATEWAY_APP
    assert body["obo_token_for_gateway"]["scp"] == "Agents.Invoke"
    assert body["obo_token_for_gateway"]["oid"] == OID == body["token_for_chat_app"]["oid"]
    assert body["obo_token_for_gateway"]["groups"] == 3


@respx.mock
def test_chat_calls_the_gateway_with_the_obo_token():
    route = respx.post(f"{GATEWAY}/agents/cve-triage/runs").mock(
        return_value=httpx.Response(200, json={"output": "done", "run_id": "r1"})
    )
    res = client().post(
        "/api/chat",
        headers=auth(),
        json={"agent": "cve-triage", "input": "hi", "history": [{"role": "user", "content": "x"}]},
    )
    assert res.status_code == 200 and res.json()["output"] == "done"
    sent = route.calls[0].request
    gateway_token = sent.headers["authorization"].split()[1]
    claims = jwt.decode(gateway_token, options={"verify_signature": False})
    assert claims["aud"] == GATEWAY_APP and claims["oid"] == OID  # OBO token, not the user's
    assert b'"history"' in sent.content


@respx.mock
def test_gateway_limits_pass_through():
    respx.post(f"{GATEWAY}/agents/cve-triage/runs").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "21"},
            json={"error": {"code": "rate_limited", "limit": "user", "retry_after": 21}},
        )
    )
    res = client().post("/api/chat", headers=auth(), json={"agent": "cve-triage", "input": "hi"})
    assert res.status_code == 429 and res.headers["retry-after"] == "21"
    assert res.json()["error"]["limit"] == "user"


@respx.mock
def test_apim_error_shape_is_normalised():
    respx.get(f"{GATEWAY}/agents/").mock(
        return_value=httpx.Response(401, json={"statusCode": 401, "message": "Sign in"})
    )
    res = client().get("/api/agents", headers=auth())
    assert res.status_code == 401 and res.json()["error"]["message"] == "Sign in"


def test_agent_names_are_validated():
    res = client().post("/api/chat", headers=auth(), json={"agent": "../admin", "input": "hi"})
    assert res.status_code == 422
