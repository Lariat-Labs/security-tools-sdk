"""The agent loop: Chat Completions with secsdk operations as function tools.

Every model call carries the user's context headers so APIM can apply that user's
limits. The loop enforces the agent's per-run budget itself and stops cleanly
before the gateway's hard per-run limit would reject a call.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from secsdk import Client, CoreSettings, telemetry
from secsdk.registry import default_registry
from secsdk.result import Result
from secsdk.tools import tool_definition

from .agents import AgentDefinition

RETRY_PREFIX = "x-agw-retry-after-"
REMAINING_HEADERS = {
    "x-agw-user-minute-remaining": "user_minute",
    "x-agw-user-daily-remaining": "user_daily",
    "x-agw-user-monthly-remaining": "user_monthly",
    "x-agw-team-monthly-remaining": "team_monthly",
    "x-agw-agent-daily-remaining": "agent_daily",
    "x-agw-run-remaining": "run",
}
LIMIT_MESSAGES = {
    "user": "You've reached your per-minute or daily token limit.",
    "user-month": "You've used your monthly token allowance.",
    "team": "Your team has used its monthly token pool.",
    "agent": "This agent is at its capacity limit.",
    "run": "This run reached its token limit.",
    "deployment": "The model is at capacity.",
}


# ----- errors ---------------------------------------------------------------------------------


class GatewayError(Exception):
    """The gateway or model refused a call for a reason other than a token limit."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


class GatewayLimitError(GatewayError):
    """A token limit was hit: 429 for a per-minute rate, 403 for a quota."""

    def __init__(self, status: int, limit: str, retry_after: int | None) -> None:
        code = "rate_limited" if status == 429 else "quota_exceeded"
        message = LIMIT_MESSAGES.get(limit, "A token limit was reached.")
        if retry_after:
            message += f" Try again in {retry_after} seconds."
        super().__init__(status, code, message)
        self.limit, self.retry_after = limit, retry_after

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "limit": self.limit, "retry_after": self.retry_after}


def classify_gateway_error(
    status: int, headers: Mapping[str, str], body: Any
) -> GatewayError | None:
    """Turn a gateway error response into a typed error (None if it isn't a gateway error)."""
    lowered = {k.lower(): v for k, v in headers.items()}
    for name, value in lowered.items():
        if name.startswith(RETRY_PREFIX) and status in (403, 429):
            try:
                retry = int(float(value))
            except ValueError:
                retry = None
            return GatewayLimitError(status, name[len(RETRY_PREFIX) :], retry)
    if status == 429:  # the model itself (deployment TPM), not a gateway counter
        retry_raw = lowered.get("retry-after")
        retry = (
            int(float(retry_raw)) if retry_raw and retry_raw.replace(".", "").isdigit() else None
        )
        return GatewayLimitError(429, "deployment", retry)
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("code"):
        return GatewayError(status, str(error["code"]), str(error.get("message", "")))
    return None


# ----- the model client -------------------------------------------------------------------------


@dataclass
class ModelReply:
    message: dict[str, Any]  # the assistant message, ready to append to the history
    usage: dict[str, int]
    headers: dict[str, str]


class ModelClient(Protocol):
    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_completion_tokens: int,
        headers: dict[str, str],
    ) -> ModelReply: ...


class GatewayModelClient:
    """OpenAI-compatible Chat Completions through the APIM models API.

    Authenticates with the runtime's managed identity (``AZURE_CLIENT_ID``) for the
    gateway app's audience; APIM checks the token's app role before anything else.
    """

    def __init__(
        self, base_url: str, scope: str, credential: Any = None, http_client: Any = None
    ) -> None:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import OpenAI

        token = get_bearer_token_provider(credential or DefaultAzureCredential(), scope)
        # No SDK retries: the loop decides what to do with 429s using the gateway's headers.
        self._client = OpenAI(
            base_url=base_url, api_key=token, max_retries=0, timeout=90, http_client=http_client
        )

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_completion_tokens: int,
        headers: dict[str, str],
    ) -> ModelReply:
        import openai

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_completion_tokens,
            "extra_headers": headers,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            raw = self._client.chat.completions.with_raw_response.create(**kwargs)
        except openai.APIStatusError as exc:
            classified = classify_gateway_error(exc.status_code, exc.response.headers, exc.body)
            if classified:
                raise classified from exc
            raise GatewayError(exc.status_code, "model_error", str(exc)) from exc
        except openai.APIConnectionError as exc:
            raise GatewayError(502, "gateway_unreachable", str(exc)) from exc
        completion = raw.parse()
        reply = completion.choices[0].message
        # Only the fields the API accepts back in the history.
        message: dict[str, Any] = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in reply.tool_calls
                if call.type == "function"
            ]
        usage = completion.usage.model_dump(exclude_none=True) if completion.usage else {}
        return ModelReply(
            message=message,
            usage={k: v for k, v in usage.items() if isinstance(v, int)},
            headers=dict(raw.headers),
        )


# ----- the run --------------------------------------------------------------------------------


@dataclass(frozen=True)
class UserContext:
    """Who is running the agent; set by APIM from the user's validated token."""

    user: str
    tier: str
    team: str
    run_id: str
    allowed_agents: tuple[str, ...] = ()

    def headers(self, agent: str) -> dict[str, str]:
        return {
            "x-agw-user": self.user,
            "x-agw-tier": self.tier,
            "x-agw-team": self.team,
            "x-agw-agent": agent,
            "x-agw-run": self.run_id,
        }


@dataclass
class ToolCallRecord:
    name: str
    operation: str | None
    ok: bool
    error_code: str | None
    ms: int


@dataclass
class RunResult:
    run_id: str
    agent: str
    output: str
    stop_reason: str  # completed | max_turns | budget_exhausted
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    limits: dict[str, int] = field(default_factory=dict)  # remaining tokens per gateway counter

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@contextmanager
def _span(name: str, **attrs: Any) -> Iterator[Any]:
    if not telemetry.enabled():
        yield None
        return
    from opentelemetry import trace

    with trace.get_tracer("agent-gateway-runtime").start_as_current_span(name) as span:
        _set(span, **attrs)
        yield span


def _set(span: Any, **attrs: Any) -> None:
    if span is not None:
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(key, value)


class AgentRunner:
    def __init__(
        self,
        model: ModelClient,
        sdk: Client | None = None,
        *,
        max_wait: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.sdk = sdk or Client(CoreSettings())
        self.max_wait = max_wait  # wait out short per-minute limits instead of failing
        self.sleep = sleep

    def run(
        self,
        agent: AgentDefinition,
        ctx: UserContext,
        user_input: str,
        history: list[dict[str, str]] | None = None,
    ) -> RunResult:
        tools_by_name = agent.tool_names()
        reg = default_registry()
        tools = [tool_definition(reg.operation(op), "openai") for op in tools_by_name.values()]
        messages: list[dict[str, Any]] = [{"role": "system", "content": agent.instructions}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history or []]
        messages.append({"role": "user", "content": user_input})

        result = RunResult(run_id=ctx.run_id, agent=agent.name, output="", stop_reason="max_turns")
        usage = result.usage
        usage.update(prompt_tokens=0, completion_tokens=0, total_tokens=0, model_calls=0)
        next_prompt_estimate = 0

        with _span(
            f"invoke_agent {agent.name}",
            **{
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": agent.name,
                "gen_ai.request.model": agent.deployment,
                "agw.user.id": ctx.user,
                "agw.tier": ctx.tier,
                "agw.team": ctx.team,
                "agw.run_id": ctx.run_id,
            },
        ) as run_span:
            for _turn in range(agent.max_turns):
                remaining = agent.budget_tokens - usage["total_tokens"]
                if usage["model_calls"] and next_prompt_estimate + 256 > remaining:
                    result.stop_reason = "budget_exhausted"
                    break
                max_out = min(agent.max_output_tokens, max(256, remaining - next_prompt_estimate))
                reply = self._complete(agent, ctx, messages, tools, max_out)

                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    usage[key] += reply.usage.get(key, 0)
                usage["model_calls"] += 1
                result.limits.update(_remaining(reply.headers))

                message = reply.message
                messages.append(message)
                if message.get("content"):
                    result.output = message["content"]
                calls = message.get("tool_calls") or []
                if not calls:
                    result.stop_reason = "completed"
                    break

                added = 0
                for call in calls:
                    content, record = self._run_tool(agent, tools_by_name, call)
                    result.tool_calls.append(record)
                    messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": content}
                    )
                    added += len(content)
                next_prompt_estimate = (
                    reply.usage.get("prompt_tokens", 0)
                    + reply.usage.get("completion_tokens", 0)
                    + added // 4
                )

            if result.stop_reason != "completed" and not result.output:
                result.output = (
                    "The run stopped before the agent finished "
                    f"({result.stop_reason.replace('_', ' ')}). Narrow the request and try again."
                )
            _set(
                run_span,
                **{
                    "gen_ai.usage.input_tokens": usage["prompt_tokens"],
                    "gen_ai.usage.output_tokens": usage["completion_tokens"],
                    "agw.stop_reason": result.stop_reason,
                    "agw.tool_calls": len(result.tool_calls),
                },
            )
        return result

    def _complete(
        self,
        agent: AgentDefinition,
        ctx: UserContext,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_out: int,
    ) -> ModelReply:
        waited = False
        while True:
            with _span(
                f"chat {agent.deployment}",
                **{
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": agent.deployment,
                    "gen_ai.request.max_tokens": max_out,
                },
            ) as span:
                try:
                    reply = self.model.complete(
                        model=agent.deployment,
                        messages=messages,
                        tools=tools,
                        max_completion_tokens=max_out,
                        headers=ctx.headers(agent.name),
                    )
                except GatewayLimitError as exc:
                    _set(span, **{"error.type": exc.code, "agw.limit": exc.limit})
                    # One short wait for per-minute limits; quotas and long waits go back to the user.
                    if (
                        exc.status == 429
                        and not waited
                        and exc.retry_after is not None
                        and exc.retry_after <= self.max_wait
                    ):
                        waited = True
                        self.sleep(exc.retry_after)
                        continue
                    raise
                _set(
                    span,
                    **{
                        "gen_ai.usage.input_tokens": reply.usage.get("prompt_tokens"),
                        "gen_ai.usage.output_tokens": reply.usage.get("completion_tokens"),
                    },
                )
                return reply

    def _run_tool(
        self, agent: AgentDefinition, tools_by_name: dict[str, str], call: dict[str, Any]
    ) -> tuple[str, ToolCallRecord]:
        name = call.get("function", {}).get("name", "")
        operation = tools_by_name.get(name)
        started = time.monotonic()
        with _span(
            f"execute_tool {name}",
            **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name},
        ) as span:
            if operation is None:
                result = Result(
                    ok=False,
                    operation=name,
                    error={
                        "code": "unknown_operation",
                        "message": f"{name} is not available to this agent",
                    },
                )
            else:
                try:
                    params = json.loads(call.get("function", {}).get("arguments") or "{}")
                    if not isinstance(params, dict):
                        raise ValueError("arguments must be a JSON object")
                except ValueError as exc:
                    result = Result(
                        ok=False,
                        operation=operation,
                        error={"code": "invalid_input", "message": f"Bad arguments: {exc}"},
                    )
                else:
                    result = self.sdk.execute(operation, params)
            content = _envelope(result, agent.max_tool_chars)
            error_code = None if result.ok else (result.error or {}).get("code")
            _set(span, **{"secsdk.operation": operation, "error.type": error_code})
        record = ToolCallRecord(
            name=name,
            operation=operation,
            ok=result.ok,
            error_code=error_code,
            ms=int((time.monotonic() - started) * 1000),
        )
        return content, record


def _envelope(result: Result, limit: int) -> str:
    text = json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return json.dumps(
        {
            "ok": False,
            "operation": result.operation,
            "error": {
                "code": "result_too_large",
                "message": f"Result was {len(text)} characters (limit {limit}).",
                "hint": "Narrow the query: lower 'limit', shorten 'days', or add filters.",
            },
        }
    )


def _remaining(headers: Mapping[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, value in headers.items():
        key = REMAINING_HEADERS.get(name.lower())
        if key:
            try:
                out[key] = int(float(value))
            except ValueError:
                continue
    return out
