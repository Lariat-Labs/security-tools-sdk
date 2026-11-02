"""Tests for running the runtime container locally: dev token credential, run_local.py,
client.py --local (offline)."""

import base64
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
import respx

GATEWAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_DIR / "runtime"))
sys.path.insert(0, str(GATEWAY_DIR))

from agw_runtime import credentials  # noqa: E402
from agw_runtime.loop import GatewayError  # noqa: E402

SCOPE = "https://cognitiveservices.azure.com/.default"


def jwt(**claims) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def fresh(**extra) -> str:
    return jwt(aud="https://cognitiveservices.azure.com", exp=int(time.time()) + 3600, **extra)


# ----- the credential --------------------------------------------------------------------------


def test_no_dev_token_means_default_credential():
    from azure.identity import DefaultAzureCredential

    assert isinstance(credentials.credential_from_env(SCOPE, {}), DefaultAzureCredential)


def test_dev_token_credential_returns_the_token_until_it_expires(monkeypatch):
    token = fresh()
    cred = credentials.credential_from_env(SCOPE, {"AGW_DEV_ACCESS_TOKEN": token})
    assert isinstance(cred, credentials.DevTokenCredential)
    assert cred.get_token(SCOPE).token == token

    from azure.core.exceptions import ClientAuthenticationError

    monkeypatch.setattr(credentials.time, "time", lambda: cred.expires_on + 1)
    with pytest.raises(ClientAuthenticationError, match=r"run_local\.py"):
        cred.get_token(SCOPE)


def test_dev_token_is_refused_in_container_apps():
    env = {"AGW_DEV_ACCESS_TOKEN": fresh(), "CONTAINER_APP_NAME": "ca-agw-runtime"}
    with pytest.raises(RuntimeError, match="local containers only"):
        credentials.credential_from_env(SCOPE, env)


@pytest.mark.parametrize(
    ("token", "message"),
    [
        ("not-a-jwt", "not a JWT"),
        (jwt(aud="https://cognitiveservices.azure.com"), "no expiry"),
        (jwt(aud="https://management.azure.com", exp=int(time.time()) + 3600), "AGW_GATEWAY_SCOPE"),
        (jwt(aud="https://cognitiveservices.azure.com", exp=int(time.time()) - 10), "expired"),
    ],
)
def test_bad_dev_tokens_fail_at_start(token, message):
    with pytest.raises(ValueError, match=message):
        credentials.credential_from_env(SCOPE, {"AGW_DEV_ACCESS_TOKEN": token})


def test_audience_matches_app_id_uri_scopes():
    token = jwt(aud="30394bae-0fe8-446e-8c01-468b732f437a", exp=int(time.time()) + 3600)
    scope = "api://30394bae-0fe8-446e-8c01-468b732f437a/.default"
    assert credentials.credential_from_env(scope, {"AGW_DEV_ACCESS_TOKEN": token})


def test_model_client_turns_a_token_failure_into_a_gateway_error(monkeypatch):
    import httpx2

    from agw_runtime.loop import GatewayModelClient

    sent = []
    cred = credentials.DevTokenCredential(fresh())
    monkeypatch.setattr(credentials.time, "time", lambda: cred.expires_on + 1)
    client = GatewayModelClient(
        "https://res.openai.azure.com/openai/v1",
        SCOPE,
        credential=cred,
        http_client=httpx2.Client(transport=httpx2.MockTransport(sent.append)),
    )
    with pytest.raises(GatewayError) as err:
        client.complete(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_completion_tokens=100,
            headers={},
        )
    assert err.value.status == 502 and err.value.code == "model_auth_failed"
    assert "expired" in err.value.message and not sent  # no request without a token


def test_app_with_a_dev_token_fails_at_start_up(monkeypatch):
    from agw_runtime.app import create_app

    monkeypatch.setenv("AGW_MODEL_BASE_URL", "https://res.openai.azure.com/openai/v1")
    monkeypatch.setenv("AGW_GATEWAY_SCOPE", SCOPE)
    monkeypatch.setenv("AGW_DEV_ACCESS_TOKEN", jwt(aud="https://management.azure.com", exp=1))
    with pytest.raises(ValueError):
        create_app()

    monkeypatch.setenv("AGW_DEV_ACCESS_TOKEN", fresh())
    from fastapi.testclient import TestClient

    assert TestClient(create_app()).get("/healthz").json()["ok"] is True


# ----- run_local.py ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://ais.services.ai.azure.com",
        "https://ais.services.ai.azure.com/",
        "https://ais.services.ai.azure.com/api/projects/agents",
        "https://ais.services.ai.azure.com/openai",
        "https://ais.services.ai.azure.com/openai/v1/",
    ],
)
def test_model_base_url(endpoint):
    import run_local

    assert run_local.model_base_url(endpoint) == "https://ais.services.ai.azure.com/openai/v1"


def test_run_local_keeps_the_token_off_the_command_line(monkeypatch):
    import run_local

    calls = []

    class Done:
        returncode = 0
        stdout = fresh()
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Done()

    monkeypatch.setattr(run_local.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(run_local.subprocess, "run", fake_run)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
    assert (
        run_local.main(["--endpoint", "https://ais.services.ai.azure.com", "--port", "8123"]) == 0
    )

    (az_cmd, _), (docker_cmd, docker_kwargs) = calls
    assert az_cmd[:3] == ["az", "account", "get-access-token"] and SCOPE in az_cmd
    assert docker_cmd[:2] == ["docker", "run"] and docker_cmd[-1] == run_local.IMAGE
    assert "127.0.0.1:8123:8000" in docker_cmd
    assert Done.stdout not in " ".join(docker_cmd)  # passed by name, value in the env
    assert docker_kwargs["env"]["AGW_DEV_ACCESS_TOKEN"] == Done.stdout
    assert (
        docker_kwargs["env"]["AGW_MODEL_BASE_URL"] == "https://ais.services.ai.azure.com/openai/v1"
    )
    passed = [docker_cmd[i + 1] for i, a in enumerate(docker_cmd) if a == "-e"]
    assert {"AGW_DEV_ACCESS_TOKEN", "APPLICATIONINSIGHTS_CONNECTION_STRING"} <= set(passed)
    assert "AGW_CAPTURE_CONVERSATION" not in passed  # content capture stays opt-in


def test_run_local_passes_conversation_capture(monkeypatch):
    import run_local

    calls = []

    class Done:
        returncode = 0
        stdout = fresh()
        stderr = ""

    monkeypatch.setattr(run_local.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        run_local.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or Done()
    )
    monkeypatch.delenv("AGW_CAPTURE_CONVERSATION", raising=False)
    monkeypatch.setenv("AGW_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
    run_local.main(["--endpoint", "https://ais.services.ai.azure.com", "--conversation"])

    docker_cmd, docker_kwargs = calls[-1]
    passed = [docker_cmd[i + 1] for i, a in enumerate(docker_cmd) if a == "-e"]
    assert {"AGW_CAPTURE_CONVERSATION", "AGW_CAPTURE_CONTENT"} <= set(passed)
    assert docker_kwargs["env"]["AGW_CAPTURE_CONVERSATION"] == "true"


# ----- client.py --local -----------------------------------------------------------------------


@respx.mock
def test_client_local_sends_the_user_context_apim_would():
    import client

    respx.get("http://127.0.0.1:8000/healthz").respond(
        json={"ok": True, "agents": ["cve-triage", "threat-brief"]}
    )
    run = respx.post("http://127.0.0.1:8000/agents/cve-triage/runs").respond(
        json={
            "output": "done",
            "stop_reason": "completed",
            "usage": {"total_tokens": 1, "model_calls": 1},
            "tool_calls": [],
            "limits": {},
        }
    )
    assert client.main(["--local", "run", "cve-triage", "hi", "--session", "s-1"]) == 0
    sent = run.calls.last.request.headers
    assert "authorization" not in sent
    assert sent["x-agw-allowed-agents"] == "cve-triage,threat-brief"
    assert sent["x-agw-user"].startswith("local:") and sent["x-agw-run"]
    assert sent["x-agw-session"] == "s-1"


def test_client_local_has_no_me():
    import client

    with pytest.raises(SystemExit):
        client.main(["--local", "me"])


@respx.mock
def test_client_local_explains_a_missing_runtime():
    import client

    respx.get("http://127.0.0.1:8000/healthz").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SystemExit, match=r"run_local\.py"):
        client.main(["--local", "list"])
