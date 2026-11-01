"""Run the agent runtime container on this machine, against your Foundry deployment.

    az login
    export AGW_FOUNDRY_ENDPOINT="https://<ais-account>.services.ai.azure.com"   # or read from Terraform
    export APPLICATIONINSIGHTS_CONNECTION_STRING="..."                       # optional: telemetry
    python deploy/agent-gateway/run_local.py --build
    python deploy/agent-gateway/client.py --local run cve-triage "Prioritise CVE-2024-3400"

The container gets a short-lived token from your Azure CLI sign-in for the model endpoint
only (about an hour; re-run to refresh). It never sees your refresh token: no ~/.azure
mount and no Azure CLI in the image, which stays the same image that ships to Azure.

Model calls go straight to Foundry, not through APIM, so there are no per-user limits and
Foundry sees your identity. You need an inference role on the account (Foundry User, or
Cognitive Services OpenAI User). The port is bound to 127.0.0.1 only: the runtime trusts
the x-agw-* user headers, which APIM would normally set.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SCOPE = "https://cognitiveservices.azure.com/.default"
IMAGE = "agw-runtime:local"
CONTAINER = "agw-runtime-local"
# Content-capture settings passed to the container when set here (agw_runtime/content.py).
CAPTURE_VARS = ("AGW_CAPTURE_CONVERSATION", "AGW_CAPTURE_CONTENT", "AGW_REDACT_ARGS")


def model_base_url(endpoint: str) -> str:
    """The OpenAI v1 URL for a Foundry resource or project endpoint."""
    url = endpoint.strip().rstrip("/")
    if not url.startswith("https://"):
        raise ValueError(f"Foundry endpoint must start with https://, got {endpoint!r}")
    url = url.split("/api/projects/", 1)[0]
    if url.endswith("/openai/v1"):
        return url
    return url.removesuffix("/openai") + "/openai/v1"


def terraform_endpoint() -> str | None:
    """The Foundry project endpoint from this deployment's Terraform outputs, if available."""
    if not shutil.which("terraform"):
        return None
    out = subprocess.run(
        ["terraform", f"-chdir={HERE / 'terraform'}", "output", "-raw", "foundry_project_endpoint"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() if out.returncode == 0 and out.stdout.startswith("https://") else None


def dev_token() -> str:
    out = subprocess.run(
        [
            "az",
            "account",
            "get-access-token",
            "--scope",
            SCOPE,
            "--query",
            "accessToken",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit(
            f"Couldn't get a token from the Azure CLI (run `az login`):\n{out.stderr.strip()}"
        )
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--endpoint",
        default=os.environ.get("AGW_FOUNDRY_ENDPOINT"),
        help="Foundry resource or project endpoint (default: AGW_FOUNDRY_ENDPOINT, then Terraform output)",
    )
    ap.add_argument("--port", type=int, default=8000, help="Local port (default: 8000)")
    ap.add_argument("--image", default=IMAGE, help=f"Image to run (default: {IMAGE})")
    ap.add_argument("--build", action="store_true", help="Build the image from this checkout first")
    ap.add_argument(
        "--conversation",
        action="store_true",
        help="record each run's input and reply on its trace, for Foundry's conversation view "
        "(AGW_CAPTURE_CONVERSATION=true)",
    )
    args = ap.parse_args(argv)

    if not shutil.which("docker"):
        raise SystemExit("Docker is not installed or not on PATH.")
    endpoint = args.endpoint or terraform_endpoint()
    if not endpoint:
        ap.error("set AGW_FOUNDRY_ENDPOINT or pass --endpoint (no Terraform output found)")
    try:
        base_url = model_base_url(endpoint)
    except ValueError as exc:
        ap.error(str(exc))

    if args.build:
        dockerfile = HERE / "runtime" / "Dockerfile"
        subprocess.run(
            ["docker", "build", "-t", args.image, "-f", str(dockerfile), str(REPO_ROOT)], check=True
        )

    # Values go through this process's environment and `-e NAME`, so the token never
    # appears on a command line (ps, shell history).
    env = {
        **os.environ,
        "AGW_DEV_ACCESS_TOKEN": dev_token(),
        "AGW_MODEL_BASE_URL": base_url,
        "AGW_GATEWAY_SCOPE": SCOPE,
        "AGW_PUBLIC_BASE_URL": f"http://127.0.0.1:{args.port}",
        "OTEL_SERVICE_NAME": "agent-gateway-runtime-local",
    }
    telemetry_vars = [
        name
        for name in ("APPLICATIONINSIGHTS_CONNECTION_STRING", "OTEL_EXPORTER_OTLP_ENDPOINT")
        if os.environ.get(name)
    ]
    passed = [
        "AGW_DEV_ACCESS_TOKEN",
        "AGW_MODEL_BASE_URL",
        "AGW_GATEWAY_SCOPE",
        "AGW_PUBLIC_BASE_URL",
        "OTEL_SERVICE_NAME",
        *telemetry_vars,
    ]
    # What the runtime records beyond metadata (see agw_runtime/content.py), as in Azure.
    if args.conversation:
        env["AGW_CAPTURE_CONVERSATION"] = "true"
    capture_vars = [name for name in CAPTURE_VARS if env.get(name)]
    passed += capture_vars
    telemetry = "on" if telemetry_vars else "off (set APPLICATIONINSIGHTS_CONNECTION_STRING)"
    if telemetry_vars:
        conversation = env.get("AGW_CAPTURE_CONVERSATION", "").lower() in ("1", "true", "yes", "on")
        telemetry += " · conversation: " + ("on" if conversation else "off (--conversation)")

    print(f"▶ {args.image} on http://127.0.0.1:{args.port}  →  {base_url}", file=sys.stderr)
    print(f"  token: about 1 hour, re-run to refresh · telemetry: {telemetry}", file=sys.stderr)
    cmd = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        CONTAINER,
        "-p",
        f"127.0.0.1:{args.port}:8000",
    ]
    for name in passed:
        cmd += ["-e", name]
    return subprocess.run([*cmd, args.image], env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
