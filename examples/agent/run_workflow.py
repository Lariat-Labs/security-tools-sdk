"""Run a secctl workflow with a Claude agent.

    uv run --group examples python examples/agent/run_workflow.py --list
    uv run --group examples python examples/agent/run_workflow.py cve-triage \
        --input cves="CVE-2024-3400 CVE-2023-4966 CVE-2021-44228"

Needs ANTHROPIC_API_KEY (or an `ant auth login` profile). The report goes to stdout
and to examples/output/, alongside a JSON log of every secctl command the agent ran.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import anthropic

from secsdk import telemetry

sys.path.insert(0, str(Path(__file__).resolve().parent))

from secctl_agent import DEFAULT_MODEL, WORKFLOWS_DIR, SecctlAgent, Workflow

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
        "--model", default=DEFAULT_MODEL, help=f"Claude model id (default: {DEFAULT_MODEL})"
    )
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
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

    exporter = telemetry.configure(service_name="secctl-agent")
    if exporter:
        print(f"  telemetry → {exporter}", file=sys.stderr)
    print(f"▶ {workflow.name} with {args.model} (effort={args.effort})", file=sys.stderr)
    try:
        agent = SecctlAgent(model=args.model, effort=args.effort, verbose=not args.quiet)
        result = agent.run(workflow, inputs)
    except (anthropic.CredentialsError, anthropic.AuthenticationError) as exc:
        print(
            f"No usable Claude API credentials ({exc}).\n"
            "Set ANTHROPIC_API_KEY (e.g. in your shell, not in the repo) or run `ant auth login`.",
            file=sys.stderr,
        )
        return 3
    except anthropic.RateLimitError as exc:
        retry = exc.response.headers.get("retry-after", "a few")
        print(f"Rate limited by the Claude API; retry in {retry} seconds.", file=sys.stderr)
        return 4
    except anthropic.APIStatusError as exc:
        print(
            f"Claude API error {exc.status_code}: {exc.message} (request id: {exc.request_id})",
            file=sys.stderr,
        )
        return 4
    except anthropic.APIConnectionError as exc:
        print(f"Could not reach the Claude API: {exc}", file=sys.stderr)
        return 4

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
                "model": args.model,
                "effort": args.effort,
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
