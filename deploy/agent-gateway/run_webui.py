"""Chat with the gateway's agents in Open WebUI, on this machine.

    az login
    python deploy/agent-gateway/run_webui.py            # deployed gateway, as you
    python deploy/agent-gateway/run_webui.py --local    # runtime container from run_local.py
    open http://127.0.0.1:3000

Starts Open WebUI in Docker and a small OpenAI-compatible adapter on 127.0.0.1 that turns
each chat turn into an agent run. Every agent appears as a model.

By default the adapter calls the deployed agents API in APIM with your Azure CLI token
(like client.py), so your tier's limits apply and the run shows up in App Insights,
Event Hubs usage and the workbooks exactly as a Teams or Copilot Studio run would.
The gateway URL and app id come from AGW_GATEWAY_URL / AGW_GATEWAY_APP_ID, else from
Terraform outputs. Open WebUI runs without sign-in: whoever opens it acts as you.

``--local`` targets a runtime started by run_local.py instead (no gateway limits).

Ctrl-C stops both; chats are kept in the agw-webui-data Docker volume.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGE = "ghcr.io/open-webui/open-webui:v0.12.5"
CONTAINER = "agw-webui"
VOLUME = "agw-webui-data"


def terraform_output(name: str) -> str | None:
    if not shutil.which("terraform"):
        return None
    out = subprocess.run(
        ["terraform", f"-chdir={HERE / 'terraform'}", "output", "-raw", name],
        capture_output=True,
        text=True,
        check=False,
    )
    value = out.stdout.strip()
    return value if out.returncode == 0 and value and "\n" not in value else None


def webui_env(adapter_url: str, key: str, port: int) -> dict[str, str]:
    """Open WebUI settings: one OpenAI connection (the adapter) and nothing else."""
    return {
        "OPENAI_API_BASE_URL": adapter_url,
        "OPENAI_API_KEY": key,
        "ENABLE_OLLAMA_API": "false",
        # Sends the chat id, which becomes the gateway session id.
        "ENABLE_FORWARD_USER_INFO_HEADERS": "true",
        # Settings come from here on every start, not from Open WebUI's database.
        "ENABLE_PERSISTENT_CONFIG": "false",
        "WEBUI_AUTH": "false",  # local and single-user: the adapter acts as you anyway
        "WEBUI_NAME": "Agent gateway",
        # Open WebUI would otherwise send extra "model" calls (titles, tags, follow-ups,
        # autocomplete) that would each run an agent and spend your tokens.
        "ENABLE_TITLE_GENERATION": "false",
        "ENABLE_TAGS_GENERATION": "false",
        "ENABLE_FOLLOW_UP_GENERATION": "false",
        "ENABLE_AUTOCOMPLETE_GENERATION": "false",
        "ENABLE_RETRIEVAL_QUERY_GENERATION": "false",
        "ENABLE_SEARCH_QUERY_GENERATION": "false",
        "ENABLE_WEB_SEARCH": "false",
        "ENABLE_EVALUATION_ARENA_MODELS": "false",
        "ENABLE_CODE_INTERPRETER": "false",
        "ANONYMIZED_TELEMETRY": "false",
        "DO_NOT_TRACK": "true",
        "SCARF_NO_ANALYTICS": "true",
        "PORT": str(port),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--local", action="store_true", help="use a local runtime (run_local.py)")
    ap.add_argument(
        "--local-url",
        default=os.environ.get("AGW_LOCAL_URL", "http://127.0.0.1:8000"),
        help="the local runtime's URL (default: AGW_LOCAL_URL or http://127.0.0.1:8000)",
    )
    ap.add_argument("--gateway", default=os.environ.get("AGW_GATEWAY_URL"))
    ap.add_argument("--app-id", default=os.environ.get("AGW_GATEWAY_APP_ID"))
    ap.add_argument("--port", type=int, default=3000, help="Open WebUI port (default: 3000)")
    ap.add_argument("--adapter-port", type=int, default=8200, help="adapter port (default: 8200)")
    ap.add_argument("--image", default=IMAGE, help=f"Open WebUI image (default: {IMAGE})")
    ap.add_argument("--no-webui", action="store_true", help="run only the adapter")
    args = ap.parse_args(argv)

    if args.local:
        os.environ.update(AGW_WEBUI_TARGET="local", AGW_LOCAL_URL=args.local_url)
        target = f"local runtime {args.local_url} (no gateway limits)"
    else:
        gateway = args.gateway or terraform_output("gateway_url")
        app_id = args.app_id or terraform_output("gateway_app_id")
        if not gateway or not app_id:
            ap.error("set AGW_GATEWAY_URL and AGW_GATEWAY_APP_ID (no Terraform outputs found)")
        os.environ.update(
            AGW_WEBUI_TARGET="gateway", AGW_GATEWAY_URL=gateway, AGW_GATEWAY_APP_ID=app_id
        )
        target = f"{gateway}/agents as your Azure CLI sign-in"
    key = secrets.token_urlsafe(24)  # only this Open WebUI may use the adapter
    os.environ["AGW_WEBUI_KEY"] = key
    os.environ.setdefault("OTEL_SERVICE_NAME", "agent-gateway-webui-local")

    started = False
    if not args.no_webui:
        if not shutil.which("docker"):
            raise SystemExit("Docker is not installed or not on PATH (or use --no-webui).")
        # Docker Desktop reaches the host's loopback through host.docker.internal; on Linux
        # the container shares the host network instead, so the adapter stays on 127.0.0.1.
        linux = sys.platform.startswith("linux")
        host = "127.0.0.1" if linux else "host.docker.internal"
        env = {**os.environ, **webui_env(f"http://{host}:{args.adapter_port}/v1", key, 8080)}
        names = list(webui_env("", "", 0))
        net = ["--network", "host"] if linux else ["-p", f"127.0.0.1:{args.port}:8080"]
        if linux:
            env["PORT"] = str(args.port)
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, check=False)
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER,
            *net,
            "-v",
            f"{VOLUME}:/app/backend/data",
        ]
        for name in names:
            cmd += ["-e", name]  # values via the environment, so the key isn't on a command line
        subprocess.run([*cmd, args.image], env=env, check=True, stdout=subprocess.DEVNULL)
        started = True

    sys.path.insert(0, str(HERE / "webui"))
    import uvicorn
    from agw_webui.app import _app_from_env

    print(f"▶ agents: {target}", file=sys.stderr)
    if started:
        print(
            f"  Open WebUI: http://127.0.0.1:{args.port}  (first start takes a minute)",
            file=sys.stderr,
        )
    try:
        uvicorn.run(_app_from_env(), host="127.0.0.1", port=args.adapter_port, log_level="warning")
    finally:
        if started:
            subprocess.run(
                ["docker", "stop", "-t", "3", CONTAINER], capture_output=True, check=False
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
