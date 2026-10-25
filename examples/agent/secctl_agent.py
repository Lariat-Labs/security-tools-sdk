"""A small agent whose only tool is the `secctl` CLI.

The model is Claude through the Anthropic API by default, or a model deployment in
Azure AI Foundry (``FoundryBackend``); the loop and its rules are the same for both.

The agent follows a written workflow (a Markdown file in ``examples/workflows/``).
It cannot reach security data any other way: no web access, no shell, no SDK
import. Everything it learns comes from `secctl` output, and every command it
runs is recorded so a person can re-run it.

The loop is written by hand rather than with an SDK's tool runner because it
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


# ----- the system prompt -------------------------------------------------------------------


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


# ----- model backends ----------------------------------------------------------------------
#
# Each backend speaks one provider's API and owns the conversation history in that API's
# format. The loop in SecctlAgent only sees the provider-neutral Turn.

PROVIDERS = ("anthropic", "foundry")


@dataclass
class ToolCall:
    id: str
    args: Any
    reason: str


@dataclass
class Turn:
    """One model response, reduced to what the loop needs."""

    stop: str  # tool_use | end_turn | max_tokens | refusal | pause_turn
    text: str
    tool_calls: list[ToolCall]
    usage: dict[str, int]  # input_tokens, output_tokens, cache_read_input_tokens
    finish_reason: str  # the provider's own stop/finish reason, for telemetry
    response_model: str | None = None
    refusal: str | None = None  # why, when stop == "refusal"
    notes: list[str] = field(default_factory=list)  # e.g. server-side fallbacks


class ModelBackend:
    provider: str  # OpenTelemetry gen_ai.provider.name
    model: str

    def add_user(self, text: str) -> None:
        raise NotImplementedError

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        """Results as (tool_call_id, text, is_error), all for the previous turn."""
        raise NotImplementedError

    def step(self) -> Turn:
        """Call the model on the history so far and append its reply to the history."""
        raise NotImplementedError


class AnthropicBackend(ModelBackend):
    """Claude through the Anthropic Messages API (the default)."""

    provider = "anthropic"

    def __init__(
        self, system_text: str, client: Any = None, model: str = DEFAULT_MODEL, effort: str = "high"
    ) -> None:
        self.client: Any = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        # Stable system prompt (no timestamps) so it is cached across turns.
        self.system = [
            {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
        ]
        self.messages: list[dict[str, Any]] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        # All results for one turn go back in one user message.
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}
                    for tid, text, err in results
                ],
            }
        )

    def step(self) -> Turn:
        response = self.client.beta.messages.create(
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
            messages=self.messages,
        )
        # Append the assistant turn exactly as returned (history is append-only).
        self.messages.append({"role": "assistant", "content": response.content})

        notes = [
            f"[fallback] {b.from_.model} declined; {b.to.model} continued"
            for b in response.content
            if b.type == "fallback"
        ]
        refusal = None
        if response.stop_reason == "refusal":
            d = response.stop_details
            refusal = f"{d.category}: {d.explanation}" if d else "no details"
        tool_calls = [
            ToolCall(b.id, b.input.get("args", []), b.input.get("reason", ""))
            for b in response.content
            if b.type == "tool_use"
        ]
        stop = str(response.stop_reason)
        if stop not in ("refusal", "max_tokens", "pause_turn"):
            stop = "tool_use" if tool_calls else "end_turn"
        return Turn(
            stop=stop,
            text="\n".join(b.text for b in response.content if b.type == "text").strip(),
            tool_calls=tool_calls,
            usage={
                k: getattr(response.usage, k, 0) or 0
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens")
            },
            finish_reason=str(response.stop_reason),
            response_model=getattr(response, "model", None),
            refusal=refusal,
            notes=notes,
        )


FOUNDRY_SCOPE = "https://cognitiveservices.azure.com/.default"


def foundry_base_url(endpoint: str) -> str:
    """The OpenAI-compatible v1 base URL for a Foundry (AI Services / Azure OpenAI) endpoint.

    Accepts the resource endpoint (``https://<res>.openai.azure.com``,
    ``https://<res>.services.ai.azure.com``, ``https://<res>.cognitiveservices.azure.com``),
    a Foundry project endpoint (``.../api/projects/<project>``) or the v1 URL itself.
    """
    url = endpoint.strip().rstrip("/")
    if not url.startswith("https://"):
        raise ValueError(f"Foundry endpoint must start with https://, got {endpoint!r}")
    url = url.split("/api/projects/", 1)[0]
    if url.endswith("/openai/v1"):
        return url
    if url.endswith("/openai"):
        return url + "/v1"
    return url + "/openai/v1"


def _openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """An Anthropic tool definition as a Chat Completions function tool."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
            "strict": tool.get("strict", False),
        },
    }


class FoundryBackend(ModelBackend):
    """A model deployment in Azure AI Foundry, through its OpenAI-compatible v1 API.

    Works for Azure OpenAI deployments (GPT, o-series) and other Foundry models served
    on the same endpoint. ``model`` is the deployment name. Authenticates with Entra ID
    (``az login``, managed identity, ...) unless an API key is given.
    """

    provider = "azure.ai.openai"

    def __init__(
        self,
        system_text: str,
        client: Any = None,
        *,
        model: str,
        endpoint: str | None = None,
        api_key: str | None = None,
        effort: str | None = None,
    ) -> None:
        token: Any = api_key  # a key, or a callable returning a fresh Entra token
        if client is None:
            from openai import OpenAI

            if not endpoint:
                raise ValueError("Foundry needs an endpoint (--endpoint or FOUNDRY_ENDPOINT)")
            if not token:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider

                token = get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_SCOPE)
            client = OpenAI(base_url=foundry_base_url(endpoint), api_key=token)
        self.client = client
        self.model = model
        self.effort = (
            effort  # reasoning_effort; only sent when set (non-reasoning models reject it)
        )
        self.tools = [_openai_tool(SECCTL_TOOL)]
        # Stable prefix: Azure OpenAI caches long prompts automatically.
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_text}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        for tid, text, err in results:
            content = f"[error] {text}" if err else text
            self.messages.append({"role": "tool", "tool_call_id": tid, "content": content})

    def step(self) -> Turn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "tools": self.tools,
            "max_completion_tokens": 16000,
        }
        if self.effort:
            kwargs["reasoning_effort"] = self.effort
        completion = self.client.chat.completions.create(**kwargs)
        choice = completion.choices[0]
        reply = choice.message

        # Only the fields the API accepts back in the history.
        message: dict[str, Any] = {"role": "assistant", "content": reply.content}
        tool_calls: list[ToolCall] = []
        if reply.tool_calls:
            message["tool_calls"] = []
            for call in reply.tool_calls:
                if call.type != "function":
                    continue
                message["tool_calls"].append(
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                )
                try:
                    data = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                # Bad arguments reach check_args, which tells the model what's wrong.
                tool_calls.append(ToolCall(call.id, data.get("args"), str(data.get("reason", ""))))
        self.messages.append(message)

        finish = str(choice.finish_reason)
        refusal = getattr(reply, "refusal", None)
        if tool_calls:
            stop = "tool_use"
        elif finish == "content_filter" or refusal:
            stop = "refusal"
            refusal = refusal or "Azure content filter"
        elif finish == "length":
            stop = "max_tokens"
        else:
            stop = "end_turn"

        u = completion.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) if u else 0
        return Turn(
            stop=stop,
            text=(reply.content or "").strip(),
            tool_calls=tool_calls,
            usage={
                "input_tokens": (u.prompt_tokens if u else 0) or 0,
                "output_tokens": (u.completion_tokens if u else 0) or 0,
                "cache_read_input_tokens": cached or 0,
            },
            finish_reason=finish,
            response_model=getattr(completion, "model", None),
            refusal=refusal,
        )


def system_prompt() -> str:
    return SYSTEM_PROMPT_HEADER + (REPO_ROOT / "AGENTS.md").read_text()


# ----- the agent loop ----------------------------------------------------------------------


@dataclass
class RunResult:
    report: str
    commands: list[CommandRecord]
    stop: str
    turns: int
    usage: dict[str, int]


class SecctlAgent:
    """Runs a workflow against a model backend (Claude by default).

    ``client`` is an Anthropic client for the default backend; pass ``backend`` to use
    another provider, e.g. ``FoundryBackend(system_prompt(), model="gpt-5", endpoint=...)``.
    Backends keep history, so use a new agent (and backend) per run.
    """

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        verbose: bool = True,
        backend: ModelBackend | None = None,
    ) -> None:
        self.backend = backend or AnthropicBackend(system_prompt(), client, model, effort)
        self.model = self.backend.model
        self.verbose = verbose

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)

    def _step(self) -> Turn:
        with _span(
            f"chat {self.model}",
            **{
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": self.backend.provider,
                "gen_ai.request.model": self.model,
            },
        ) as span:
            turn = self.backend.step()
            _set(
                span,
                **{
                    "gen_ai.response.model": turn.response_model,
                    "gen_ai.response.finish_reasons": [turn.finish_reason],
                    "gen_ai.usage.input_tokens": turn.usage["input_tokens"],
                    "gen_ai.usage.output_tokens": turn.usage["output_tokens"],
                },
            )
            return turn

    def run(self, workflow: Workflow, inputs: dict[str, str], max_nudges: int = 2) -> RunResult:
        with _span(
            f"invoke_agent {workflow.name}",
            **{
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": workflow.name,
                "gen_ai.provider.name": self.backend.provider,
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
        self.backend.add_user(
            f"# Workflow: {workflow.name}\n\n{directions}\n\n"
            "Begin with step 1. Use the secctl tool for every step that needs data."
        )
        records: list[CommandRecord] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        nudges = 0
        turn: Turn | None = None

        for n in range(1, workflow.max_turns + 1):
            turn = self._step()
            for k in usage:
                usage[k] += turn.usage.get(k, 0)
            for note in turn.notes:
                self.log(f"  {note}")

            if turn.stop == "refusal":
                return RunResult(
                    f"Model declined the request ({turn.refusal}).", records, "refusal", n, usage
                )

            if turn.stop == "max_tokens":
                return RunResult(
                    turn.text + "\n\n[stopped: max_tokens]", records, "max_tokens", n, usage
                )

            if turn.stop == "pause_turn":
                continue

            if turn.tool_calls:
                results = []
                for call in turn.tool_calls:
                    text, is_error, rec = run_secctl(call.args, call.reason)
                    records.append(rec)
                    status = rec.blocked or f"exit {rec.exit_code}, {rec.seconds}s"
                    self.log(f"  $ {rec.display}   ({status})")
                    if call.reason:
                        self.log(f"    ↳ {call.reason}")
                    results.append((call.id, text, is_error))
                self.backend.add_tool_results(results)
                continue

            # end_turn with no tool calls: accept only if the required steps were run.
            missing = workflow.missing_steps(records)
            if missing and nudges < max_nudges:
                nudges += 1
                self.log(
                    f"  [check] required steps not run yet: {', '.join(missing)}. Asking the agent to finish them."
                )
                self.backend.add_user(
                    "The workflow is not complete. These required secctl commands have not been run: "
                    + ", ".join(f"`secctl {m}`" for m in missing)
                    + ". Run them (or explain from secctl output why they cannot run), then write the full report again."
                )
                continue

            report = turn.text
            if missing:
                report += (
                    "\n\n> Note: workflow finished without these required commands: "
                    + ", ".join(missing)
                )
            return RunResult(report, records, "complete" if not missing else "incomplete", n, usage)

        last = turn.text if turn is not None else ""
        return RunResult(
            last + "\n\n[stopped: reached max_turns]",
            records,
            "max_turns",
            workflow.max_turns,
            usage,
        )
