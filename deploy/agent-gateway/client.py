"""Call the agent gateway from a terminal, as yourself.

    az login
    export AGW_GATEWAY_URL="https://<apim>.azure-api.net"
    export AGW_GATEWAY_APP_ID="<gateway app registration client id>"
    python deploy/agent-gateway/client.py list
    python deploy/agent-gateway/client.py me      # tier, team, cost center, limits (GET /agents/me)
    python deploy/agent-gateway/client.py run cve-triage "Prioritise CVE-2024-3400 and CVE-2023-4966"

Uses your Azure CLI sign-in to get a token for ``api://<app-id>/.default``, which carries ``Agents.Invoke``
(the deployment pre-authorises the Azure CLI for that scope). Your tier, team and agent
access come from your Entra groups; the gateway enforces your token limits.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from azure.identity import AzureCliCredential


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--gateway", default=os.environ.get("AGW_GATEWAY_URL"))
    ap.add_argument("--app-id", default=os.environ.get("AGW_GATEWAY_APP_ID"))
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="agents you may use")
    sub.add_parser("me", help="your gateway profile: tier, team, cost center, limits")
    run = sub.add_parser("run", help="run an agent")
    run.add_argument("agent")
    run.add_argument("input")
    run.add_argument("--json", action="store_true", help="print the full response")
    run.add_argument("--session", help="conversation id (letters, digits, dashes) for telemetry")
    args = ap.parse_args(argv)
    if not args.gateway or not args.app_id:
        ap.error("set AGW_GATEWAY_URL and AGW_GATEWAY_APP_ID (or pass --gateway/--app-id)")

    # The Azure CLI credential only takes "/.default"; the token still carries Agents.Invoke
    # because the deployment pre-authorises the Azure CLI for that scope.
    token = AzureCliCredential().get_token(f"api://{args.app_id}/.default").token
    headers = {"Authorization": f"Bearer {token}"}
    if getattr(args, "session", None):
        headers["x-agw-session"] = args.session
    base = args.gateway.rstrip("/") + "/agents"
    with httpx.Client(headers=headers, timeout=300) as http:
        if args.command == "list":
            res = http.get(base + "/")
        elif args.command == "me":
            res = http.get(base + "/me")
        else:
            res = http.post(f"{base}/{args.agent}/runs", json={"input": args.input})

    try:
        body = res.json()
    except ValueError:
        print(f"HTTP {res.status_code}: {res.text[:500]}", file=sys.stderr)
        return 1
    if res.status_code >= 400:
        error = body.get("error", body) if isinstance(body, dict) else body
        print(f"HTTP {res.status_code}: {json.dumps(error)}", file=sys.stderr)
        return 3 if res.status_code in (401, 403) else 4
    if args.command == "me":
        print(json.dumps(body, indent=2))
    elif args.command == "list":
        for agent in body:
            print(f"{agent['name']:<20} {agent['description']}")
    elif args.json:
        print(json.dumps(body, indent=2))
    else:
        print(body["output"])
        usage, limits = body["usage"], body.get("limits", {})
        print(
            f"\n[{body['stop_reason']}] {usage['total_tokens']} tokens in {usage['model_calls']} "
            f"model calls, {len(body['tool_calls'])} tool calls. Remaining: "
            + ", ".join(f"{k} {v:,}" for k, v in limits.items()),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
