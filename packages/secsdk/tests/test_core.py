"""Core machinery: operations, validation, envelopes, registry, HTTP behaviour."""

from typing import Annotated

import httpx
import pytest
import respx
from pydantic import BaseModel, Field

from secsdk import (
    AuthStatus,
    Client,
    ConfigError,
    InvalidInputError,
    NotFoundError,
    Provider,
    Registry,
    UpstreamError,
    default_registry,
    operation,
)
from secsdk.tools import tool_definitions


class Echo(BaseModel):
    text: str
    times: int


class EchoProvider(Provider):
    name = "echo"
    title = "Echo"
    description = "Test provider."

    @operation(examples=["secctl echo say hi"])
    def say(
        self,
        text: Annotated[str, Field(min_length=1, description="What to say")],
        times: Annotated[int, Field(ge=1, le=3)] = 1,
    ) -> Echo:
        """Echo text back.

        Longer description here.
        """
        return Echo(text=text * times, times=times)

    @operation(auth=True)
    def secret(self) -> str:
        """Needs credentials."""
        return "s3cret"

    def auth_status(self) -> AuthStatus:
        return AuthStatus(required=True, configured=False, missing=["ECHO_KEY"])


@pytest.fixture
def reg():
    r = Registry()
    r.register(EchoProvider)
    return r


@pytest.fixture
def echo_client(settings, reg):
    with Client(settings, registry=reg) as c:
        yield c


def test_operation_metadata():
    op = EchoProvider.operations["say"]
    assert op.fqname == "echo.say"
    assert op.tool_name == "echo_say"
    assert op.summary == "Echo text back."
    assert op.description == "Longer description here."
    params = {p["name"]: p for p in op.parameters()}
    assert params["text"]["required"] and params["text"]["description"] == "What to say"
    assert params["times"]["default"] == 1
    schema = op.input_schema()
    assert schema["required"] == ["text"]
    assert schema["additionalProperties"] is False


def test_direct_call_and_call_by_name(echo_client):
    assert echo_client.echo.say("ab", times=2).text == "abab"
    assert echo_client.call("echo.say", text="x").times == 1


def test_validation_errors_are_invalid_input(echo_client):
    with pytest.raises(InvalidInputError) as exc:
        echo_client.call("echo.say", text="x", times=9)
    assert exc.value.details[0]["param"] == "times"
    with pytest.raises(InvalidInputError):
        echo_client.call("echo.say", text="x", bogus=1)  # extra params are rejected
    with pytest.raises(InvalidInputError):
        echo_client.echo.say()  # missing positional


def test_auth_required(echo_client):
    with pytest.raises(ConfigError) as exc:
        echo_client.echo.secret()
    assert "ECHO_KEY" in exc.value.message


def test_execute_envelope(echo_client):
    ok = echo_client.execute("echo.say", {"text": "hi"})
    assert ok.ok and ok.exit_code == 0
    assert ok.to_dict()["data"] == {"text": "hi", "times": 1}
    bad = echo_client.execute("echo.secret")
    assert not bad.ok and bad.exit_code == 3
    assert bad.to_dict()["error"]["code"] == "not_configured"
    unknown = echo_client.execute("echo.nope")
    assert unknown.exit_code == 2 and "say" in unknown.error["hint"]


def test_registry_rejects_duplicates(reg):
    class Other(Provider):
        name = "echo"

    with pytest.raises(ValueError):
        reg.register(Other)


def test_builtin_providers_discovered():
    names = default_registry().names()
    for expected in (
        "nvd",
        "kev",
        "epss",
        "osint",
        "sec",
        "feeds",
        "feedly",
        "tenable",
        "azure",
        "intel",
    ):
        assert expected in names
    # every built-in operation must produce a valid schema and a summary
    for op in default_registry().operations():
        assert op.summary, op.fqname
        assert op.input_schema()["type"] == "object"


def test_tool_definitions_styles(reg):
    anthropic = tool_definitions(reg, "anthropic")
    assert anthropic[0]["name"] == "echo_say" and "input_schema" in anthropic[0]
    openai = tool_definitions(reg, "openai")
    assert openai[0]["function"]["parameters"]["properties"]["text"]


@respx.mock
def test_http_retries_then_succeeds(client):
    route = respx.get("https://api.first.org/data/v1/epss").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"data": []})]
    )
    assert client.epss.score(["CVE-2021-44228"]) == []
    assert route.call_count == 2


@respx.mock
def test_http_error_mapping(client):
    respx.get("https://api.first.org/data/v1/epss").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(UpstreamError) as exc:
        client.epss.score(["CVE-2021-44228"])
    assert exc.value.status == 500


@respx.mock
def test_not_found_mapping(client):
    respx.get(url__startswith="https://rdap.org/domain/").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFoundError):
        client.osint.whois("example.com")


@respx.mock
def test_cache_roundtrip(tmp_path):
    from secsdk import CoreSettings

    route = respx.get(
        "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
    ).mock(return_value=httpx.Response(200, json={"vulnerabilities": []}))
    s = CoreSettings(cache_enabled=True, cache_dir=tmp_path / "c", rate_limit=False)
    with Client(s) as c:
        c.kev.search()
        c.kev.search()
    assert route.call_count == 1
