"""Run a secctl workflow with an agent: Claude (default) or an Azure AI Foundry model.

    uv run --group examples python examples/agent/run_workflow.py --list
    uv run --group examples python examples/agent/run_workflow.py cve-triage \
        --input cves="CVE-2024-3400 CVE-2023-4966 CVE-2021-44228"

    # A model deployment in Azure AI Foundry instead of the Anthropic API:
    export FOUNDRY_ENDPOINT="https://<resource>.openai.azure.com"   # or a project endpoint
    export FOUNDRY_MODEL="<deployment name>"
    uv run --group examples python examples/agent/run_workflow.py cve-triage \
        --provider foundry --input cves="CVE-2024-3400"

Anthropic needs ANTHROPIC_API_KEY (or an `ant auth login` profile). Foundry uses your
Entra ID sign-in (`az login`; needs an inference role such as Foundry User or Cognitive
Services OpenAI User on the resource), or FOUNDRY_API_KEY if set. The report goes to
stdout and to examples/output/, alongside a JSON log of every secctl command the agent ran.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from secsdk import telemetry

sys.path.insert(0, str(Path(__file__).resolve().parent))

from secctl_agent import (
    DEFAULT_MODEL,
    PROVIDERS,
    WORKFLOWS_DIR,
    FoundryBackend,
    SecctlAgent,
    Workflow,
    system_prompt,
)

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output"


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--input expects key=value, got {pair!r}")
        out[key.strip()] = value.strip()
    return out


def list_workflows() -> None:
    for path in sorted(WORKFLOWS_DIR.glob("*.md")):
        wf = Workflow.load(str(path))
        print(f"{wf.name:18} {wf.description}")
        for key, spec in wf.inputs.items():
            default = f" (default: {spec['default']})" if "default" in spec else " (required)"
            print(f"{'':18}   --input {key}=...  {spec.get('description', '')}{default}")


def build_agent(args: argparse.Namespace) -> SecctlAgent:
    """The agent for --provider; raises SystemExit on missing configuration."""
    verbose = not args.quiet
    if args.provider == "anthropic":
        if args.effort == "minimal":
            raise SystemExit(
                "--effort minimal is for Foundry reasoning models; Claude takes low..max"
            )
        return SecctlAgent(model=args.model, effort=args.effort or "high", verbose=verbose)
    if args.effort == "max":
        raise SystemExit(
            "--effort max is Claude-only; Foundry reasoning models take minimal..xhigh"
        )
    if not args.endpoint:
        raise SystemExit("--provider foundry needs --endpoint or FOUNDRY_ENDPOINT")
    if not args.model:
        raise SystemExit("--provider foundry needs --model <deployment name> or FOUNDRY_MODEL")
    try:
        backend = FoundryBackend(
            system_prompt(),
            model=args.model,
            endpoint=args.endpoint,
            api_key=os.environ.get("FOUNDRY_API_KEY") or None,
            effort=args.effort,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return SecctlAgent(backend=backend, verbose=verbose)


def api_error(exc: Exception, provider: str) -> tuple[str, int] | None:
    """A message and exit code for a model API error, or None if it isn't one."""
    name = "Claude API" if provider == "anthropic" else "Foundry"
    credentials: tuple[type[BaseException], ...]
    if provider == "anthropic":
        sdk: Any = anthropic
        credentials = (anthropic.CredentialsError, anthropic.AuthenticationError)
        hint = (
            "Set ANTHROPIC_API_KEY (e.g. in your shell, not in the repo) or run `ant auth login`."
        )
    else:
        import openai
        from azure.core.exceptions import ClientAuthenticationError

        sdk = openai
        credentials = (openai.AuthenticationError, ClientAuthenticationError)
        hint = "Run `az login` (or set FOUNDRY_API_KEY), and check you have an inference role on the resource."
    if isinstance(exc, credentials):
        return f"No usable {name} credentials ({exc}).\n{hint}", 3
    if isinstance(exc, sdk.RateLimitError):
        retry = exc.response.headers.get("retry-after", "a few")
        return f"Rate limited by {name}; retry in {retry} seconds.", 4
    if isinstance(exc, sdk.APIStatusError):
        return (
            f"{name} error {exc.status_code}: {exc.message} (request id: {exc.request_id})",
            4,
        )
    if isinstance(exc, sdk.APIConnectionError):
        return f"Could not reach {name}: {exc}", 4
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "workflow", nargs="?", help="Workflow name (see --list) or path to a workflow .md file"
    )
    ap.add_argument(
        "--input",
        "-i",
        action="append",
        default=[],
        help="Workflow input as key=value (repeatable)",
    )
    ap.add_argument("--list", action="store_true", help="List available workflows and their inputs")
    ap.add_argument(
        "--provider",
        choices=PROVIDERS,
        default=os.environ.get("SECCTL_AGENT_PROVIDER", "anthropic"),
        help="Model provider (default: anthropic, or SECCTL_AGENT_PROVIDER)",
    )
    ap.add_argument(
        "--model",
        help=f"Claude model id (default: {DEFAULT_MODEL}) or Foundry deployment name "
        "(default: FOUNDRY_MODEL)",
    )
    ap.add_argument(
        "--endpoint",
        default=os.environ.get("FOUNDRY_ENDPOINT"),
        help="Foundry resource or project endpoint (default: FOUNDRY_ENDPOINT)",
    )
    ap.add_argument(
        "--effort",
        choices=["minimal", "low", "medium", "high", "xhigh", "max"],
        help="Claude effort (default: high) or Foundry reasoning_effort (default: not sent; "
        "set it only for reasoning models)",
    )
    ap.add_argument(
        "--out", type=Path, default=OUTPUT_DIR, help="Where to save the report and command log"
    )
    ap.add_argument("--quiet", action="store_true", help="Don't print commands as they run")
    args = ap.parse_args()

    if args.list or not args.workflow:
        list_workflows()
        return 0

    workflow = Workflow.load(args.workflow)
    inputs = parse_inputs(args.input)
    workflow.render(inputs)  # fail fast on missing inputs, before any API call

    if not args.model:
        args.model = (
            DEFAULT_MODEL if args.provider == "anthropic" else os.environ.get("FOUNDRY_MODEL")
        )
    agent = build_agent(args)

    exporter = telemetry.configure(service_name="secctl-agent")
    if exporter:
        print(f"  telemetry → {exporter}", file=sys.stderr)
    effort = args.effort or ("high" if args.provider == "anthropic" else "default")
    print(f"▶ {workflow.name} with {args.provider}:{args.model} (effort={effort})", file=sys.stderr)
    try:
        result = agent.run(workflow, inputs)
    except Exception as exc:
        handled = api_error(exc, args.provider)
        if handled is None:
            raise
        message, code = handled
        print(message, file=sys.stderr)
        return code

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / f"{workflow.name}-{stamp}.md"
    log_path = args.out / f"{workflow.name}-{stamp}.commands.json"
    report_path.write_text(result.report + "\n")
    log_path.write_text(
        json.dumps(
            {
                "workflow": workflow.name,
                "inputs": inputs,
                "provider": args.provider,
                "model": args.model,
                "effort": effort,
                "stop": result.stop,
                "turns": result.turns,
                "usage": result.usage,
                "commands": [asdict(c) | {"command": c.display} for c in result.commands],
            },
            indent=2,
        )
    )

    print(result.report)
    print(
        f"\n✔ {result.stop} · {result.turns} turns · {len(result.commands)} secctl commands · "
        f"{result.usage['input_tokens']} in / {result.usage['output_tokens']} out tokens "
        f"({result.usage['cache_read_input_tokens']} cached)\n  report: {report_path}\n  log:    {log_path}",
        file=sys.stderr,
    )
    return 0 if result.stop == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
