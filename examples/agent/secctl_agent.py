"""A small Claude agent whose only tool is the `secctl` CLI.

The agent follows a written workflow (a Markdown file in ``examples/workflows/``).
It cannot reach security data any other way: no web access, no shell, no SDK
import. Everything it learns comes from `secctl` output, and every command it
runs is recorded so a person can re-run it.

The loop is written by hand rather than with the SDK's tool runner because it
enforces one rule the runner cannot: a final answer is not accepted until the
workflow's required `secctl` commands have actually been run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import yaml

from secsdk import telemetry

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / "examples" / "workflows"

DEFAULT_MODEL = "claude-opus-5-5"
MAX_OUTPUT_CHARS = 20_000  # per command; the model is told when output is cut
COMMAND_TIMEOUT = 300  # seconds; Tenable exports and crt.sh can be slow

# Commands the agent may not run: `dev` writes files into the repo, `cache`
# changes local state. `--env-file` is blocked so the agent can't choose which
# credentials it loads.
BLOCKED_COMMANDS = {"dev", "cache"}
BLOCKED_FLAGS = {"--env-file"}
GLOBAL_OPTIONS_WITH_VALUE = {"-f", "--format"}


@contextmanager
def _span(name: str, **attrs: Any) -> Iterator[Any]:
    """An OpenTelemetry span when telemetry is on, otherwise nothing.

    Attribute names follow the OpenTelemetry GenAI conventions where they exist.
    """
    if not telemetry.enabled():
        yield None
        return
    from opentelemetry import trace

    with trace.get_tracer("secctl-agent").start_as_current_span(name) as span:
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, v)
        yield span


def _set(span: Any, **attrs: Any) -> None:
    if span is not None:
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, v)


# ----- the tool ---------------------------------------------------------------------------

SECCTL_TOOL: dict[str, Any] = {
    "name": "secctl",
    "description": (
        "Run the secctl security CLI and return its exit code and output. This is your only "
        "source of security data. Pass the arguments that follow `secctl`, one list item per "
        "argument, exactly as you would type them in a shell (no quoting needed). Examples: "
        '["guide"], ["providers"], ["describe", "kev.lookup"], '
        '["kev", "lookup", "CVE-2023-4966"], ["intel", "cve-priority", "CVE-2024-3400", "--no-nvd"]. '
        "Output is a JSON envelope; check `ok` and, on failure, follow `error.hint`. "
        "Exit codes: 0 ok, 2 invalid input, 3 not configured, 4 upstream error, 5 not found."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Arguments after `secctl`, e.g. ["kev", "search", "--days", "7"]',
            },
            "reason": {
                "type": "string",
                "description": "One short sentence: which workflow step this command serves.",
            },
        },
        "required": ["args", "reason"],
        "additionalProperties": False,
    },
}


def secctl_command() -> list[str]:
    """How to launch secctl: the installed script, or the module via this Python."""
    exe = shutil.which("secctl")
    return [exe] if exe else [sys.executable, "-m", "secctl"]


def command_words(args: list[str]) -> list[str]:
    """The command path without options, e.g. ['-f', 'table', 'kev', 'lookup', 'X'] -> ['kev', 'lookup', 'X']."""
    words: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in GLOBAL_OPTIONS_WITH_VALUE:
            skip = True
            continue
        if a.startswith("-"):
            continue
        words.append(a)
    return words


def check_args(args: Any) -> str | None:
    """Return an error message if the arguments are not allowed, else None."""
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        return "`args` must be a non-empty list of strings."
    if len(args) > 60 or any(len(a) > 4000 for a in args):
        return "Too many or too long arguments."
    words = command_words(args)
    if words and words[0] in BLOCKED_COMMANDS:
        return f"`secctl {words[0]}` is not available to the agent."
    if any(a.split("=", 1)[0] in BLOCKED_FLAGS for a in args):
        return "`--env-file` is not available to the agent; credentials come from the environment."
    return None


@dataclass
class CommandRecord:
    args: list[str]
    reason: str
    exit_code: int | None
    seconds: float
    output_chars: int
    truncated: bool
    blocked: str | None = None

    @property
    def display(self) -> str:
        return "secctl " + " ".join(_shell_quote(a) for a in self.args)


def run_secctl(args: list[str], reason: str = "") -> tuple[str, bool, CommandRecord]:
    """Run one secctl command. Returns (tool_result_text, is_error, record).

    The process runs without a shell, so arguments can't inject other commands.
    """
    problem = check_args(args)
    if problem:
        rec = CommandRecord(
            args=list(args) if isinstance(args, list) else [],
            reason=reason,
            exit_code=None,
            seconds=0.0,
            output_chars=0,
            truncated=False,
            blocked=problem,
        )
        return f"Command not run: {problem}", True, rec

    with _span(
        "execute_tool secctl",
        **{
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "secctl",
            "secctl.command": " ".join(command_words(args)[:2]),
        },
    ) as span:
        text, is_error, rec = _exec_secctl(args, reason)
        _set(span, **{"secctl.exit_code": rec.exit_code, "secctl.blocked": rec.blocked})
        return text, is_error, rec


def _exec_secctl(args: list[str], reason: str) -> tuple[str, bool, CommandRecord]:
    # Passing TRACEPARENT makes secctl's own spans children of this tool call.
    env = telemetry.inject_env({**os.environ, "SECCTL_FORMAT": "json", "NO_COLOR": "1"})
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [*secctl_command(), *args],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            env=env,
            cwd=REPO_ROOT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        rec = CommandRecord(
            args=args,
            reason=reason,
            exit_code=None,
            seconds=COMMAND_TIMEOUT,
            output_chars=0,
            truncated=False,
            blocked="timeout",
        )
        return (
            f"Command timed out after {COMMAND_TIMEOUT}s. Try a narrower query (e.g. --limit, --days).",
            True,
            rec,
        )

    elapsed = time.monotonic() - start
    out = proc.stdout
    truncated = len(out) > MAX_OUTPUT_CHARS
    if truncated:
        out = (
            out[:MAX_OUTPUT_CHARS]
            + f"\n[output cut at {MAX_OUTPUT_CHARS} of {len(proc.stdout)} characters; "
            "re-run with --fields, --limit or a narrower filter to see the rest]"
        )
    text = f"exit_code: {proc.returncode}\n{out}"
    if proc.stderr.strip():
        text += f"\nstderr:\n{proc.stderr.strip()[:2000]}"
    rec = CommandRecord(
        args=args,
        reason=reason,
        exit_code=proc.returncode,
        seconds=round(elapsed, 2),
        output_chars=len(proc.stdout),
        truncated=truncated,
    )
    # A non-zero exit (e.g. 5 = not found) is information for the model, not a tool failure.
    return text, False, rec


def _shell_quote(a: str) -> str:
    return a if a and all(c.isalnum() or c in "-_./:=,@" for c in a) else json.dumps(a)


# ----- workflows ---------------------------------------------------------------------------


@dataclass
class Workflow:
    name: str
    description: str
    directions: str
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    max_turns: int = 30

    @classmethod
    def load(cls, name_or_path: str) -> Workflow:
        path = Path(name_or_path)
        if not path.exists():
            path = WORKFLOWS_DIR / f"{name_or_path}.md"
        if not path.exists():
            available = ", ".join(sorted(p.stem for p in WORKFLOWS_DIR.glob("*.md")))
            raise SystemExit(f"Unknown workflow '{name_or_path}'. Available: {available}")
        text = path.read_text()
        if not text.startswith("---"):
            raise SystemExit(f"{path}: missing YAML front matter")
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
        return cls(
            name=meta.get("name", path.stem),
            description=meta.get("description", ""),
            directions=body.strip(),
            inputs=meta.get("inputs") or {},
            required=meta.get("required_commands") or [],
            max_turns=int(meta.get("max_turns", 30)),
        )

    def render(self, values: dict[str, str]) -> str:
        """Fill {placeholders} in the directions from inputs (or their defaults)."""
        filled: dict[str, str] = {}
        for key, spec in self.inputs.items():
            if key in values:
                filled[key] = values[key]
            elif "default" in spec:
                filled[key] = str(spec["default"])
            else:
                raise SystemExit(
                    f"Workflow '{self.name}' needs --input {key}=...  ({spec.get('description', '')})"
                )
        unknown = set(values) - set(self.inputs)
        if unknown:
            raise SystemExit(f"Unknown input(s) for '{self.name}': {', '.join(sorted(unknown))}")
        return self.directions.format_map(filled)

    def missing_steps(self, records: list[CommandRecord]) -> list[str]:
        """Required command prefixes (e.g. 'intel cve-priority') not yet run successfully."""
        ran = [" ".join(command_words(r.args)) for r in records if r.blocked is None]
        return [
            req
            for req in self.required
            if not any(r == req or r.startswith(req + " ") for r in ran)
        ]


# ----- the agent loop ----------------------------------------------------------------------


SYSTEM_PROMPT_HEADER = """\
You are a security analyst agent. You complete the workflow you are given by running the \
`secctl` CLI through the `secctl` tool, then you write the requested report.

Ground rules:
- `secctl` is your only source of security facts. Do not state a fact (a score, a date, a \
KEV listing, a filing) that you did not read in secctl output during this session.
- Follow the workflow's steps in order. If a step can't be completed (provider not \
configured, upstream error), say so in the report and continue with the remaining steps.
- Every operation is read-only and passive. You have no other tools.
- Keep tool output small with --fields, --limit and --compact.
- Your final message is the report itself, in Markdown. Cite the secctl command behind \
each finding.

The repository's usage rules for agents (AGENTS.md) follow.

"""


@dataclass
class RunResult:
    report: str
    commands: list[CommandRecord]
    stop: str
    turns: int
    usage: dict[str, int]


class SecctlAgent:
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        verbose: bool = True,
    ) -> None:
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.verbose = verbose
        agents_md = (REPO_ROOT / "AGENTS.md").read_text()
        # Stable system prompt (no timestamps) so it is cached across turns.
        self.system = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT_HEADER + agents_md,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)

    def _create(self, messages: list[dict[str, Any]]) -> Any:
        with _span(
            f"chat {self.model}",
            **{
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "anthropic",
                "gen_ai.request.model": self.model,
            },
        ) as span:
            response = self._call_model(messages)
            _set(
                span,
                **{
                    "gen_ai.response.model": getattr(response, "model", None),
                    "gen_ai.response.finish_reasons": [str(response.stop_reason)],
                    "gen_ai.usage.input_tokens": getattr(response.usage, "input_tokens", None),
                    "gen_ai.usage.output_tokens": getattr(response.usage, "output_tokens", None),
                },
            )
            return response

    def _call_model(self, messages: list[dict[str, Any]]) -> Any:
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            # Thinking is adaptive by default on Claude Opus 5.5; effort sets how hard it works.
            output_config={"effort": self.effort},
            # If a safety classifier declines (security topics can trip the cyber
            # classifier), the API retries on a suitable fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=self.system,
            tools=[SECCTL_TOOL],
            messages=messages,
        )

    def run(self, workflow: Workflow, inputs: dict[str, str], max_nudges: int = 2) -> RunResult:
        with _span(
            f"invoke_agent {workflow.name}",
            **{
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": workflow.name,
                "gen_ai.request.model": self.model,
            },
        ) as span:
            result = self._run(workflow, inputs, max_nudges)
            _set(
                span,
                **{
                    "secctl_agent.stop": result.stop,
                    "secctl_agent.turns": result.turns,
                    "secctl_agent.commands": len(result.commands),
                    "gen_ai.usage.input_tokens": result.usage["input_tokens"],
                    "gen_ai.usage.output_tokens": result.usage["output_tokens"],
                },
            )
            return result

    def _run(self, workflow: Workflow, inputs: dict[str, str], max_nudges: int) -> RunResult:
        directions = workflow.render(inputs)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"# Workflow: {workflow.name}\n\n{directions}\n\n"
                    "Begin with step 1. Use the secctl tool for every step that needs data."
                ),
            }
        ]
        records: list[CommandRecord] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        nudges = 0
        response = None

        for turn in range(1, workflow.max_turns + 1):
            response = self._create(messages)
            for k in usage:
                usage[k] += getattr(response.usage, k, 0) or 0
            # Append the assistant turn exactly as returned (history is append-only).
            messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "fallback":
                    self.log(
                        f"  [fallback] {block.from_.model} declined; {block.to.model} continued"
                    )

            if response.stop_reason == "refusal":
                details = response.stop_details
                why = f"{details.category}: {details.explanation}" if details else "no details"
                return RunResult(
                    f"Model declined the request ({why}).", records, "refusal", turn, usage
                )

            if response.stop_reason == "max_tokens":
                return RunResult(
                    _text(response) + "\n\n[stopped: max_tokens]",
                    records,
                    "max_tokens",
                    turn,
                    usage,
                )

            if response.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if tool_uses:
                results = []
                for tu in tool_uses:
                    args = tu.input.get("args", [])
                    reason = tu.input.get("reason", "")
                    text, is_error, rec = run_secctl(args, reason)
                    records.append(rec)
                    status = rec.blocked or f"exit {rec.exit_code}, {rec.seconds}s"
                    self.log(f"  $ {rec.display}   ({status})")
                    if reason:
                        self.log(f"    ↳ {reason}")
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": text,
                            "is_error": is_error,
                        }
                    )
                # All results for this turn go back in one user message.
                messages.append({"role": "user", "content": results})
                continue

            # end_turn with no tool calls: accept only if the required steps were run.
            missing = workflow.missing_steps(records)
            if missing and nudges < max_nudges:
                nudges += 1
                self.log(
                    f"  [check] required steps not run yet: {', '.join(missing)}. Asking the agent to finish them."
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The workflow is not complete. These required secctl commands have not been run: "
                            + ", ".join(f"`secctl {m}`" for m in missing)
                            + ". Run them (or explain from secctl output why they cannot run), then write the full report again."
                        ),
                    }
                )
                continue

            report = _text(response)
            if missing:
                report += (
                    "\n\n> Note: workflow finished without these required commands: "
                    + ", ".join(missing)
                )
            return RunResult(
                report, records, "complete" if not missing else "incomplete", turn, usage
            )

        last = _text(response) if response is not None else ""
        return RunResult(
            last + "\n\n[stopped: reached max_turns]",
            records,
            "max_turns",
            workflow.max_turns,
            usage,
        )


def _text(response: Any) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()
