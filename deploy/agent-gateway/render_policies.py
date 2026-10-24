"""Render the agent gateway's Azure API Management policies from gateway.yaml.

    uv run python deploy/agent-gateway/render_policies.py deploy/agent-gateway/gateway.example.yaml
    uv run python deploy/agent-gateway/render_policies.py my-gateway.yaml --out build/policies
    uv run python deploy/agent-gateway/render_policies.py gateway.yaml --check   # CI: fail if stale

Two policies are produced:

* ``agents-api.xml``: the front door users call. Validates the user's Entra token,
  resolves tier, team and allowed agents from group claims, refuses users without a
  tier or entitlement, limits runs per minute, and forwards trusted ``x-agw-*``
  context headers to the agent runtime.
* ``models-api.xml``: the only route from the runtime to the models. Accepts only
  the runtime's managed identity, then applies token limits per deployment, agent,
  run, user (minute/day/month) and team (month), emits token metrics and writes a
  usage event per call to Event Hubs.

Limits are written into the policy as literals because APIM's ``tokens-per-minute``
attribute doesn't accept expressions. Environment-specific ids (tenant, app
registration, managed identities) stay as APIM named values (``{{agw-...}}``) that
the Bicep template creates, so the rendered files are the same in every environment
that shares a gateway.yaml.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Annotated
from xml.sax.saxutils import escape

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# Names end up inside C# string literals in the policy, so they are restricted to a
# safe alphabet rather than escaped.
Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
GroupId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    ),
]
Tokens = Annotated[int, Field(gt=0)]
CostCenter = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,12}$")]

USER_SCOPE = "Agents.Invoke"  # delegated scope users' tokens must carry
INTERNAL_ROLE = "Gateway.Internal"  # app role held by the APIM and runtime identities
Q = '"'  # a C# double quote, for building expressions
CONTEXT_HEADERS = (
    "x-agw-user",
    "x-agw-tier",
    "x-agw-team",
    "x-agw-agent",
    "x-agw-run",
    "x-agw-session",
)
# A client-chosen conversation id is kept only if it looks like one.
SESSION_PATTERN = "^[A-Za-z0-9-]{1,64}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Tier(_Model):
    name: Name
    group_id: GroupId
    tokens_per_minute: Tokens
    tokens_per_day: Tokens
    tokens_per_month: Tokens

    @model_validator(mode="after")
    def _ordered(self) -> Tier:
        if not self.tokens_per_minute <= self.tokens_per_day <= self.tokens_per_month:
            raise ValueError(f"tier {self.name}: expected per-minute <= per-day <= per-month")
        return self


def _digits(value: object) -> object:
    """Cost centres are numbers; YAML may give an int."""
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class Team(_Model):
    name: Name
    group_id: GroupId
    tokens_per_month: Tokens
    cost_center: CostCenter = Field(description="Numeric cost centre for chargeback")

    _cc = field_validator("cost_center", mode="before")(_digits)


class DefaultTeam(_Model):
    name: Name
    tokens_per_month: Tokens
    cost_center: CostCenter | None = None

    _cc = field_validator("cost_center", mode="before")(_digits)


class Agent(_Model):
    name: Name
    group_id: GroupId
    deployments: list[Name] = Field(min_length=1)
    tokens_per_minute: Tokens
    tokens_per_day: Tokens
    tokens_per_run: Tokens


class Deployment(_Model):
    name: Name
    tokens_per_minute: Tokens


class GatewayConfig(_Model):
    runs_per_minute: Tokens = 10
    tiers: list[Tier] = Field(min_length=1)
    teams: list[Team] = []
    default_team: DefaultTeam
    agents: list[Agent] = Field(min_length=1)
    deployments: list[Deployment] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> GatewayConfig:
        for label, names in (
            ("tier", [t.name for t in self.tiers]),
            ("team", [t.name for t in self.teams] + [self.default_team.name]),
            ("agent", [a.name for a in self.agents]),
            ("deployment", [d.name for d in self.deployments]),
        ):
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(f"duplicate {label} names: {', '.join(dupes)}")
        known = {d.name for d in self.deployments}
        for agent in self.agents:
            missing = [d for d in agent.deployments if d not in known]
            if missing:
                raise ValueError(f"agent {agent.name} uses unknown deployments: {missing}")
        return self


def load_config(path: Path) -> GatewayConfig:
    return GatewayConfig.model_validate(yaml.safe_load(path.read_text()))


# ----- XML helpers --------------------------------------------------------------------------
# Expressions go in single-quoted attributes so the C# can use double quotes. Every value
# is XML-escaped, so the output is well-formed XML (deploy it with format "xml").


def _attr(value: str) -> str:
    return "'" + escape(value, {"'": "&apos;"}) + "'"


def _expr(code: str) -> str:
    return _attr("@(" + code + ")")


def _block(code: str) -> str:
    return _attr("@{ " + code + " }")


def _var(name: str) -> str:
    return f'((string)context.Variables["{name}"])'


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.splitlines())


def _deny(status: int, reason: str, code: str, message: str) -> str:
    body = escape(json.dumps({"error": {"code": code, "message": message}}))
    return (
        "<return-response>\n"
        f'  <set-status code="{status}" reason="{reason}" />\n'
        '  <set-header name="Content-Type" exists-action="override">\n'
        "    <value>application/json</value>\n"
        "  </set-header>\n"
        f"  <set-body>{body}</set-body>\n"
        "</return-response>"
    )


def _when(condition_attr: str, body: str) -> str:
    return f"<when condition={condition_attr}>\n{_indent(body, 2)}\n</when>"


def _choose(whens: list[str], otherwise: str | None = None) -> str:
    parts = [_indent(w, 2) for w in whens]
    if otherwise is not None:
        parts.append(_indent(f"<otherwise>\n{_indent(otherwise, 2)}\n</otherwise>", 2))
    return "<choose>\n" + "\n".join(parts) + "\n</choose>"


def _token_limit(
    key_expr: str,
    *,
    per_minute: int | None = None,
    quota: int | None = None,
    period: str | None = None,
    retry_header: str,
    remaining_quota_header: str | None = None,
    remaining_minute_header: str | None = None,
) -> str:
    attrs = [f"counter-key={_expr(key_expr)}"]
    if per_minute is not None:
        attrs.append(f'tokens-per-minute="{per_minute}"')
    if quota is not None:
        attrs += [f'token-quota="{quota}"', f'token-quota-period="{period}"']
    attrs.append('estimate-prompt-tokens="false"')
    attrs.append(f'retry-after-header-name="{retry_header}"')
    if remaining_quota_header:
        attrs.append(f'remaining-quota-tokens-header-name="{remaining_quota_header}"')
    if remaining_minute_header:
        attrs.append(f'remaining-tokens-header-name="{remaining_minute_header}"')
    return "<llm-token-limit " + "\n    ".join(attrs) + " />"


def _set_header(name: str, value_expr: str | None = None, delete: bool = False) -> str:
    if delete:
        return f'<set-header name="{name}" exists-action="delete" />'
    return (
        f'<set-header name="{name}" exists-action="override">\n'
        f"  <value>{escape('@(' + str(value_expr) + ')')}</value>\n"
        "</set-header>"
    )


def _validate_token(*, user: bool) -> str:
    """Entra token validation: users need the delegated scope; internal callers the app role."""
    lines = [
        '<validate-azure-ad-token tenant-id="{{agw-tenant-id}}" output-token-variable-name="agw-jwt"',
        '    failed-validation-httpcode="401"',
        (
            '    failed-validation-error-message="Sign in with a Microsoft Entra token for the agent gateway.">'
            if user
            else '    failed-validation-error-message="Only the agent runtime may call models.">'
        ),
    ]
    if not user:
        lines += [
            "  <client-application-ids>",
            "    <application-id>{{agw-runtime-client-id}}</application-id>",
            "  </client-application-ids>",
        ]
    lines += [
        "  <audiences>",
        "    <audience>api://{{agw-gateway-app-id}}</audience>",
        "    <audience>{{agw-gateway-app-id}}</audience>",
        "  </audiences>",
        "  <required-claims>",
        (
            f'    <claim name="scp" match="any" separator=" ">\n      <value>{USER_SCOPE}</value>\n    </claim>'
            if user
            else f'    <claim name="roles" match="any">\n      <value>{INTERNAL_ROLE}</value>\n    </claim>'
        ),
        "  </required-claims>",
        "</validate-azure-ad-token>",
    ]
    return "\n".join(lines)


def _policies(inbound: list[str], outbound: list[str]) -> str:
    sections = [
        "<inbound>\n" + _indent("<base />\n" + "\n".join(inbound), 2) + "\n</inbound>",
        "<backend>\n  <base />\n</backend>",
        "<outbound>\n" + _indent("<base />\n" + "\n".join(outbound), 2) + "\n</outbound>",
        "<on-error>\n  <base />\n</on-error>",
    ]
    return "<policies>\n" + _indent("\n".join(sections), 2) + "\n</policies>\n"


def _cost_center_code(cfg: GatewayConfig, team_expr: str) -> str:
    """C# block body: the cost centre of a team name ("" if none)."""
    pairs = [(t.name, t.cost_center) for t in cfg.teams]
    if cfg.default_team.cost_center:
        pairs.append((cfg.default_team.name, cfg.default_team.cost_center))
    return (
        f"var t = {team_expr}; "
        + " ".join(f'if (t == "{name}") {{ return "{cc}"; }}' for name, cc in pairs)
        + ' return "";'
    )


def _whoami_code(cfg: GatewayConfig, claims: str) -> str:
    """C# block body: the caller's gateway profile as JSON (answered by APIM for GET /me)."""
    tier, team = _var("agw-tier"), _var("agw-team")
    limits = " ".join(
        f'if (tier == "{t.name}") {{ limits = new JObject(new JProperty("tokensPerMinute", {t.tokens_per_minute}), '
        f'new JProperty("tokensPerDay", {t.tokens_per_day}), new JProperty("tokensPerMonth", {t.tokens_per_month})); }}'
        for t in cfg.tiers
    )
    pools = [(t.name, t.tokens_per_month) for t in cfg.teams]
    pools.append((cfg.default_team.name, cfg.default_team.tokens_per_month))
    pool = " ".join(f'if (team == "{name}") {{ pool = {tokens}; }}' for name, tokens in pools)
    claim = lambda name: f"{claims}.GetValueOrDefault({Q}{name}{Q}, {Q}{Q})"  # noqa: E731
    return (
        f"var tier = {tier}; var team = {team}; var limits = new JObject(); long pool = 0; "
        f"{limits} {pool} "
        f'var agents = new JArray({_var("agw-allowed")}.Split(new[] {{ "," }}, StringSplitOptions.RemoveEmptyEntries)); '
        "return new JObject("
        'new JProperty("user", new JObject('
        f'new JProperty("id", {_var("agw-user")}), new JProperty("name", {claim("name")}), '
        f'new JProperty("upn", {claim("preferred_username")}))), '
        'new JProperty("tier", tier), new JProperty("team", team), '
        f'new JProperty("costCenter", {_var("agw-cost-center")}), '
        'new JProperty("agents", agents), '
        'new JProperty("limits", new JObject(new JProperty("user", limits), '
        f'new JProperty("teamTokensPerMonth", pool), new JProperty("runsPerMinute", {cfg.runs_per_minute})))'
        ").ToString();"
    )


# ----- the front door: users -> agent runtime ---------------------------------------------------


def render_agents_api(cfg: GatewayConfig) -> str:
    groups = _var("agw-groups")
    tier_code = (
        f"var g = {groups}; "
        + " ".join(
            f'if (g.Contains(",{t.group_id.lower()},")) {{ return "{t.name}"; }}' for t in cfg.tiers
        )
        + ' return "";'
    )
    team_code = (
        f"var g = {groups}; "
        + " ".join(
            f'if (g.Contains(",{t.group_id.lower()},")) {{ return "{t.name}"; }}' for t in cfg.teams
        )
        + f' return "{cfg.default_team.name}";'
    )
    allowed_code = (
        f'var g = {groups}; var a = ""; '
        + " ".join(
            f'if (g.Contains(",{a.group_id.lower()},")) {{ a += ",{a.name}"; }}' for a in cfg.agents
        )
        + " return a.Length > 0 ? a.Substring(1) : a;"
    )
    jwt = '((Jwt)context.Variables["agw-jwt"])'
    claims = jwt + ".Claims"
    inbound = [
        _validate_token(user=True),
        f'<set-variable name="agw-user" value={_expr(claims + ".GetValueOrDefault(" + Q + "oid" + Q + ", " + Q + Q + ")")} />',
        # Comma-separated group ids, lower-cased and wrapped in commas for exact matching.
        f'<set-variable name="agw-groups" value={_expr(Q + "," + Q + " + " + claims + ".GetValueOrDefault(" + Q + "groups" + Q + ", " + Q + Q + ").ToLowerInvariant() + " + Q + "," + Q)} />',
        f'<set-variable name="agw-tier" value={_block(tier_code)} />',
        f'<set-variable name="agw-team" value={_block(team_code)} />',
        f'<set-variable name="agw-allowed" value={_block(allowed_code)} />',
        f'<set-variable name="agw-cost-center" value={_block(_cost_center_code(cfg, _var("agw-team")))} />',
        f'<set-variable name="agw-agent" value={_expr("context.Request.MatchedParameters.GetValueOrDefault(" + Q + "agent" + Q + ", " + Q + Q + ")")} />',
        f'<set-variable name="agw-session" value={_block("var s = context.Request.Headers.GetValueOrDefault(" + Q + "x-agw-session" + Q + ", " + Q + Q + "); return System.Text.RegularExpressions.Regex.IsMatch(s, " + Q + SESSION_PATTERN + Q + ") ? s : context.RequestId.ToString();")} />',
        _choose(
            [
                _when(
                    _expr(_var("agw-tier") + ".Length == 0"),
                    _deny(
                        403,
                        "Forbidden",
                        "no_tier",
                        "Your account is not in an agent access tier. Ask for access to an agents group.",
                    ),
                ),
                _when(
                    _block(
                        f'var a = {_var("agw-agent")}; return a.Length > 0 && !("," + {_var("agw-allowed")} + ",").Contains("," + a + ",");'
                    ),
                    _deny(403, "Forbidden", "not_entitled", "You don't have access to this agent."),
                ),
            ]
        ),
        f'<rate-limit-by-key calls="{cfg.runs_per_minute}" renewal-period="60"\n'
        f"    counter-key={_expr(Q + 'runs:' + Q + ' + ' + _var('agw-user'))}\n"
        f"    increment-condition={_expr('context.Request.Method == ' + Q + 'POST' + Q)} />",
        # GET /agents/me: APIM answers from what it just derived; the runtime isn't called.
        _choose(
            [
                _when(
                    _expr("context.Operation.Id == " + Q + "whoami" + Q),
                    "<return-response>\n"
                    '  <set-status code="200" reason="OK" />\n'
                    '  <set-header name="Content-Type" exists-action="override">\n'
                    "    <value>application/json</value>\n"
                    "  </set-header>\n"
                    f"  <set-body>{escape('@{ ' + _whoami_code(cfg, claims) + ' }')}</set-body>\n"
                    "</return-response>",
                )
            ]
        ),
        # Overwrite anything the caller sent: only the gateway sets context headers.
        _set_header("x-agw-user", _var("agw-user")),
        _set_header("x-agw-tier", _var("agw-tier")),
        _set_header("x-agw-team", _var("agw-team")),
        _set_header("x-agw-allowed-agents", _var("agw-allowed")),
        _set_header("x-agw-cost-center", _var("agw-cost-center")),
        # Which app asked for the token (azp in v2 tokens, appid in v1): lets the runtime tell a
        # user's own client (delegated) from a middle tier acting for them (on-behalf-of).
        _set_header(
            "x-agw-client",
            claims
            + ".GetValueOrDefault("
            + Q
            + "azp"
            + Q
            + ", "
            + claims
            + ".GetValueOrDefault("
            + Q
            + "appid"
            + Q
            + ", "
            + Q
            + Q
            + "))",
        ),
        # Who the user is, for reports (from the validated token; never trusted for limits).
        _set_header(
            "x-agw-user-name", claims + ".GetValueOrDefault(" + Q + "name" + Q + ", " + Q + Q + ")"
        ),
        _set_header(
            "x-agw-user-upn",
            claims + ".GetValueOrDefault(" + Q + "preferred_username" + Q + ", " + Q + Q + ")",
        ),
        _set_header("x-agw-run", "context.RequestId.ToString()"),
        _set_header("x-agw-session", _var("agw-session")),
        '<set-backend-service backend-id="agw-runtime" />',
        # Replaces the user's token: the runtime only accepts the gateway's identity.
        '<authentication-managed-identity resource="api://{{agw-gateway-app-id}}" client-id="{{agw-apim-client-id}}" />',
    ]
    return _policies(inbound, [])


# ----- the model route: agent runtime -> models -------------------------------------------------


def render_models_api(cfg: GatewayConfig) -> str:
    user, tier, team, agent, run, model = (
        _var(n) for n in ("agw-user", "agw-tier", "agw-team", "agw-agent", "agw-run", "agw-model")
    )
    q = Q

    def header_var(name: str, header: str) -> str:
        return f'<set-variable name="{name}" value={_expr(f"context.Request.Headers.GetValueOrDefault({q}{header}{q}, {q}{q})")} />'

    # True when the agent may NOT use the requested deployment.
    model_refused = (
        f"var a = {agent}; var m = {model}; "
        + " ".join(
            f'if (a == "{a.name}") {{ return !('
            + " || ".join(f'm == "{d}"' for d in a.deployments)
            + "); }"
            for a in cfg.agents
        )
        + " return true;"
    )

    deployment_limits = _choose(
        [
            _when(
                _expr(f'{model} == "{d.name}"'),
                _token_limit(
                    f'"d:" + {model}',
                    per_minute=d.tokens_per_minute,
                    retry_header="x-agw-retry-after-deployment",
                ),
            )
            for d in cfg.deployments
        ],
        _deny(
            403, "Forbidden", "unknown_model", "This model is not available through the gateway."
        ),
    )
    agent_limits = _choose(
        [
            _when(
                _expr(f'{agent} == "{a.name}"'),
                _token_limit(
                    f'"a:" + {agent}',
                    per_minute=a.tokens_per_minute,
                    quota=a.tokens_per_day,
                    period="Daily",
                    retry_header="x-agw-retry-after-agent",
                    remaining_quota_header="x-agw-agent-daily-remaining",
                )
                + "\n"
                + _token_limit(
                    f'"r:" + {run}',
                    quota=a.tokens_per_run,
                    period="Daily",
                    retry_header="x-agw-retry-after-run",
                    remaining_quota_header="x-agw-run-remaining",
                ),
            )
            for a in cfg.agents
        ],
        _deny(403, "Forbidden", "unknown_agent", "Unknown agent."),
    )
    # The per-minute and daily counters include the tier, so a tier change starts fresh
    # counters (APIM v2 needs one tokens-per-minute value per counter key). The monthly
    # counter doesn't, so changing tier doesn't reset the month.
    user_limits = _choose(
        [
            _when(
                _expr(f'{tier} == "{t.name}"'),
                _token_limit(
                    f'"u:" + {user} + ":{t.name}"',
                    per_minute=t.tokens_per_minute,
                    quota=t.tokens_per_day,
                    period="Daily",
                    retry_header="x-agw-retry-after-user",
                    remaining_quota_header="x-agw-user-daily-remaining",
                    remaining_minute_header="x-agw-user-minute-remaining",
                )
                + "\n"
                + _token_limit(
                    f'"u:" + {user} + ":month"',
                    quota=t.tokens_per_month,
                    period="Monthly",
                    retry_header="x-agw-retry-after-user-month",
                    remaining_quota_header="x-agw-user-monthly-remaining",
                ),
            )
            for t in cfg.tiers
        ],
        _deny(403, "Forbidden", "unknown_tier", "Unknown tier."),
    )
    teams = [(t.name, t.tokens_per_month) for t in cfg.teams]
    teams.append((cfg.default_team.name, cfg.default_team.tokens_per_month))
    team_limits = _choose(
        [
            _when(
                _expr(f'{team} == "{name}"'),
                _token_limit(
                    f'"t:" + {team}',
                    quota=tokens,
                    period="Monthly",
                    retry_header="x-agw-retry-after-team",
                    remaining_quota_header="x-agw-team-monthly-remaining",
                ),
            )
            for name, tokens in teams
        ],
        _deny(403, "Forbidden", "unknown_team", "Unknown team."),
    )

    usage_event = (
        "var r = context.Response.StatusCode == 200 ? "
        "context.Response.Body.As<JObject>(preserveContent: true) : null; "
        'var u = r == null ? null : r["usage"] as JObject; '
        'var d = u == null ? null : u["prompt_tokens_details"] as JObject; '
        "return new JObject("
        'new JProperty("time", DateTime.UtcNow.ToString("o")), '
        'new JProperty("requestId", context.RequestId.ToString()), '
        f'new JProperty("runId", {run}), '
        f'new JProperty("sessionId", {_var("agw-session")}), '
        f'new JProperty("user", {user}), '
        f'new JProperty("tier", {tier}), '
        f'new JProperty("team", {team}), '
        f'new JProperty("costCenter", {_var("agw-cost-center")}), '
        f'new JProperty("agent", {agent}), '
        f'new JProperty("deployment", {model}), '
        'new JProperty("status", context.Response.StatusCode), '
        'new JProperty("promptTokens", u == null ? 0 : (u.Value<int?>("prompt_tokens") ?? 0)), '
        'new JProperty("completionTokens", u == null ? 0 : (u.Value<int?>("completion_tokens") ?? 0)), '
        'new JProperty("cachedTokens", d == null ? 0 : (d.Value<int?>("cached_tokens") ?? 0)), '
        'new JProperty("totalTokens", u == null ? 0 : (u.Value<int?>("total_tokens") ?? 0))'
        ").ToString(Newtonsoft.Json.Formatting.None);"
    )

    inbound = [
        _validate_token(user=False),
        header_var("agw-user", "x-agw-user"),
        header_var("agw-tier", "x-agw-tier"),
        header_var("agw-team", "x-agw-team"),
        header_var("agw-agent", "x-agw-agent"),
        header_var("agw-run", "x-agw-run"),
        header_var("agw-session", "x-agw-session"),
        # Derived from the team here rather than trusted from a header.
        f'<set-variable name="agw-cost-center" value={_block(_cost_center_code(cfg, team))} />',
        f'<set-variable name="agw-model" value={_block("var b = context.Request.Body == null ? null : context.Request.Body.As<JObject>(preserveContent: true); return b == null ? " + q + q + " : (b.Value<string>(" + q + "model" + q + ") ?? " + q + q + ");")} />',
        f'<set-variable name="agw-stream" value={_block("var b = context.Request.Body == null ? null : context.Request.Body.As<JObject>(preserveContent: true); return b != null && (b.Value<bool?>(" + q + "stream" + q + ") ?? false);")} />',
        _choose(
            [
                _when(
                    _expr(
                        " || ".join(
                            f"{v}.Length == 0" for v in (user, tier, team, agent, run, model)
                        )
                    ),
                    _deny(
                        400,
                        "Bad Request",
                        "missing_context",
                        "x-agw-* context headers and a model are required.",
                    ),
                ),
                # Streaming makes APIM estimate tokens instead of counting them, so the
                # runtime uses non-streaming calls and the gateway insists on it.
                _when(
                    _expr('(bool)context.Variables["agw-stream"]'),
                    _deny(
                        400,
                        "Bad Request",
                        "streaming_not_supported",
                        "Use non-streaming calls so token usage is exact.",
                    ),
                ),
                _when(
                    _block(model_refused),
                    _deny(
                        403, "Forbidden", "model_not_allowed", "This agent may not use that model."
                    ),
                ),
            ]
        ),
        deployment_limits,
        agent_limits,
        user_limits,
        team_limits,
        '<llm-emit-token-metric namespace="agent-gateway">\n'
        f'  <dimension name="Tier" value={_expr(tier)} />\n'
        f'  <dimension name="Team" value={_expr(team)} />\n'
        f'  <dimension name="Agent" value={_expr(agent)} />\n'
        f'  <dimension name="Deployment" value={_expr(model)} />\n'
        f'  <dimension name="CostCenter" value={_expr(_var("agw-cost-center"))} />\n'
        "</llm-emit-token-metric>",
        *(_set_header(h, delete=True) for h in CONTEXT_HEADERS),
        '<set-backend-service backend-id="agw-models" />',
        '<authentication-managed-identity resource="https://cognitiveservices.azure.com" client-id="{{agw-apim-client-id}}" />',
    ]
    outbound = [
        f'<log-to-eventhub logger-id="agw-usage">{escape("@{ " + usage_event + " }")}</log-to-eventhub>'
    ]
    return _policies(inbound, outbound)


def render(cfg: GatewayConfig) -> dict[str, str]:
    return {
        "agents-api.xml": render_agents_api(cfg),
        "models-api.xml": render_models_api(cfg),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("config", type=Path, help="gateway.yaml")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "policies",
        help="output directory (default: policies/ next to this script)",
    )
    ap.add_argument("--check", action="store_true", help="fail if the files on disk are stale")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"Invalid config {args.config}: {exc}", file=sys.stderr)
        return 2

    stale = []
    for name, text in render(cfg).items():
        path = args.out / name
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"wrote {path}")
    if stale:
        print("Stale policies (re-run without --check): " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
