"""mcp_call.py against a mocked APIM MCP endpoint (offline)."""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import respx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mcp_call

URL = "https://apim.example.net/secsdk-mcp/mcp"


def sse(payload):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "mcp-session-id": "s-1"},
        text=f"event: message\ndata: {json.dumps(payload)}\n\n",
    )


def test_parse_body_handles_json_and_sse():
    assert mcp_call.parse_body(httpx.Response(200, json={"id": 1})) == {"id": 1}
    assert mcp_call.parse_body(sse({"id": 2})) == {"id": 2}
    assert mcp_call.parse_body(httpx.Response(202)) is None


def test_full_exchange_sends_token_and_session(monkeypatch, capsys):
    monkeypatch.setattr(
        mcp_call,
        "AzureCliCredential",
        lambda: SimpleNamespace(
            get_token=lambda scope: SimpleNamespace(token="tok", expires_on=time.time() + 3600)
        ),
    )
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append((body["method"], request.headers))
        if body["method"] == "initialize":
            return sse({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "agw-mcp"}}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            tool = {"name": "kev_lookup", "inputSchema": {"properties": {"cve_id": {}}}}
            return sse({"jsonrpc": "2.0", "id": 2, "result": {"tools": [tool]}})
        envelope = {"ok": True, "operation": "kev.lookup", "data": {"cve_id": "CVE-2024-3400"}}
        text = [{"type": "text", "text": json.dumps(envelope)}]
        return sse({"jsonrpc": "2.0", "id": 3, "result": {"content": text}})

    with respx.mock:
        respx.post(URL).mock(side_effect=handler)
        assert mcp_call.main(["--url", URL, "--app-id", "app"]) == 0

    methods = [m for m, _ in sent]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert all(h["authorization"] == "Bearer tok" for _, h in sent)
    assert "mcp-session-id" not in sent[0][1] and sent[-1][1]["mcp-session-id"] == "s-1"
    out = capsys.readouterr().out
    assert "tool: kev_lookup" in out and '"operation": "kev.lookup"' in out
