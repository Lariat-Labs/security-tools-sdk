"""Call the gateway's MCP server as yourself: initialize, list tools, call one.

    az login
    uv run python deploy/agent-gateway/mcp_call.py                      # kev_lookup CVE-2024-3400
    uv run python deploy/agent-gateway/mcp_call.py --cve CVE-2023-4966
    uv run python deploy/agent-gateway/mcp_call.py --list               # tools only

The MCP endpoint and gateway app id come from AGW_MCP_URL / AGW_GATEWAY_APP_ID, else from
Terraform outputs (mcp_endpoint, gateway_app_id). Needs enable_mcp_tools = true.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx
from azure.identity import AzureCliCredential
from run_webui import terraform_output

PROTOCOL = "2025-06-18"


def parse_body(res: httpx.Response) -> Any:
    """A JSON-RPC response, sent as plain JSON or as server-sent events."""
    if "text/event-stream" in res.headers.get("content-type", ""):
        data = [line[5:].strip() for line in res.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1]) if data else None
    return res.json() if res.content else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=os.environ.get("AGW_MCP_URL"))
    ap.add_argument("--app-id", default=os.environ.get("AGW_GATEWAY_APP_ID"))
    ap.add_argument("--cve", default="CVE-2024-3400", help="CVE id for kev_lookup")
    ap.add_argument("--list", action="store_true", help="only list the tools")
    args = ap.parse_args(argv)
    url = args.url or terraform_output("mcp_endpoint")
    app_id = args.app_id or terraform_output("gateway_app_id")
    if not url or not app_id:
        ap.error("no MCP endpoint: set enable_mcp_tools = true and apply, or pass --url/--app-id")

    token = AzureCliCredential().get_token(f"api://{app_id}/.default").token
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL,
    }
    with httpx.Client(headers=headers, timeout=60) as http:

        def rpc(method: str, params: dict[str, Any], rid: int | None) -> Any:
            body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
            if rid is not None:
                body["id"] = rid
            res = http.post(url, json=body)
            if res.status_code >= 400:
                raise SystemExit(f"{method}: HTTP {res.status_code} {res.text[:500]}")
            session = res.headers.get("mcp-session-id")
            if session:
                http.headers["Mcp-Session-Id"] = session
            return parse_body(res)

        init = rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "agw-mcp-call", "version": "0.1"},
            },
            1,
        )
        server = (init or {}).get("result", {}).get("serverInfo", {})
        print(f"server: {server.get('name', '?')} {server.get('version', '')}", file=sys.stderr)
        rpc("notifications/initialized", {}, None)

        tools = rpc("tools/list", {}, 2)
        for tool in (tools or {}).get("result", {}).get("tools", []):
            print(f"tool: {tool['name']}  input: {json.dumps(tool.get('inputSchema', {}))}")
        if args.list:
            return 0

        out = rpc("tools/call", {"name": "kev_lookup", "arguments": {"cve_id": args.cve}}, 3)
        result = (out or {}).get("result") or out
        for part in (result or {}).get("content", []) if isinstance(result, dict) else []:
            if part.get("type") == "text":
                try:
                    print(json.dumps(json.loads(part["text"]), indent=2))
                except ValueError:
                    print(part["text"])
                break
        else:
            print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
