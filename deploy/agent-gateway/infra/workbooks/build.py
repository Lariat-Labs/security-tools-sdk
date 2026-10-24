"""Generate the Azure Workbook definitions deployed by main.bicep.

    uv run python deploy/agent-gateway/infra/workbooks/build.py          # write *.json
    uv run python deploy/agent-gateway/infra/workbooks/build.py --check  # CI: fail if stale

Workbooks are JSON with KQL embedded as strings; keeping the queries here as Python
keeps them readable and reviewable.

Data sources (all in the Log Analytics workspace behind Application Insights):

* ``AppDependencies``: runtime spans (``invoke_agent``, ``chat``, ``execute_tool``,
  secsdk operations and upstream HTTP calls). Metadata only.
* ``AppTraces``: span events with captured content (``agw.user_input``,
  ``agw.tool_arguments``) when ``AGW_CAPTURE_CONTENT=true``. Shorter retention.
* ``AppMetrics`` (APIM token metrics) and ``ApiManagementGatewayLogs``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
T = "TimeGenerated {TimeRange}"
WORKSPACE = "__AGW_WORKSPACE_ID__"
RUNTIME = "agent-gateway-runtime"

# Reusable KQL fragments ---------------------------------------------------------------------

RUNS = f"""let runs = AppDependencies
| where {T} and Name startswith "invoke_agent "
| extend run = tostring(Properties["agw.run_id"]), session = tostring(Properties["gen_ai.conversation.id"]),
         user = tostring(Properties["agw.user.id"]), team = tostring(Properties["agw.team"]),
         agent = tostring(Properties["gen_ai.agent.name"]), stop = tostring(Properties["agw.stop_reason"]),
         tokens = toint(Properties["gen_ai.usage.input_tokens"]) + toint(Properties["gen_ai.usage.output_tokens"]),
         tool_calls = toint(Properties["agw.tool_calls"]);
let inputs = AppTraces
| where {T} and Message == "agw.user_input"
| project run = tostring(Properties["agw.run_id"]), input = tostring(Properties["agw.input"]);
let args = AppTraces
| where {T} and Message == "agw.tool_arguments"
| project run = tostring(Properties["agw.run_id"]), arguments = tostring(Properties["gen_ai.tool.call.arguments"]);
let term = "{{Search}}";
let hits = union (inputs | where input contains term | project run), (args | where arguments contains term | project run)
| distinct run;
let filtered = runs
| where (isempty("{{User}}") or user == "{{User}}") and (isempty("{{Agent}}") or agent == "{{Agent}}")
| where isempty(term) or run in (hits);"""


def query(
    name: str,
    title: str,
    kql: str,
    viz: str = "table",
    width: int | None = None,
    **content: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": 3,
        "name": name,
        "content": {
            "version": "KqlItem/1.0",
            "query": kql.strip(),
            "size": 0,
            "title": title,
            "queryType": 0,
            "resourceType": "microsoft.operationalinsights/workspaces",
            # Replaced with the workspace id by main.bicep, so every query runs against the
            # right workspace however the workbook is opened.
            "crossComponentResources": [WORKSPACE],
            "visualization": viz,
            **content,
        },
    }
    if width:
        item["customWidth"] = str(width)
    return item


def text(name: str, markdown: str) -> dict[str, Any]:
    return {"type": 1, "name": name, "content": {"json": markdown.strip()}}


def params(*extra: dict[str, Any]) -> dict[str, Any]:
    time_range = {
        "id": "a3e1c6d2-0001-4c55-9e0a-000000000001",
        "version": "KqlParameterItem/1.0",
        "name": "TimeRange",
        "label": "Time range",
        "type": 4,
        "isRequired": True,
        "value": {"durationMs": 86400000},
        "typeSettings": {
            "selectableValues": [
                {"durationMs": 3600000},
                {"durationMs": 86400000},
                {"durationMs": 604800000},
                {"durationMs": 2592000000},
            ],
            "allowCustom": True,
        },
    }
    return {
        "type": 9,
        "name": "parameters",
        "content": {"version": "KqlParameterItem/1.0", "parameters": [time_range, *extra]},
    }


def text_param(n: int, name: str, label: str, description: str) -> dict[str, Any]:
    return {
        "id": f"a3e1c6d2-0001-4c55-9e0a-00000000000{n}",
        "version": "KqlParameterItem/1.0",
        "name": name,
        "label": label,
        "type": 1,
        "description": description,
        "value": "",
    }


def hidden_param(n: int, name: str) -> dict[str, Any]:
    return {**text_param(n, name, name, "Set by selecting a row"), "isHiddenWhenLocked": True}


def visible_when(param: str) -> dict[str, Any]:
    return {
        "conditionalVisibility": {"parameterName": param, "comparison": "isNotEqualTo", "value": ""}
    }


def notebook(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "Notebook/1.0",
        "items": list(items),
        "isLocked": False,
        "fallbackResourceIds": [],
    }


# Latest known display name / sign-in name per user object id (runs record them from v0.3).
NAMES = """let names = AppDependencies
| where TimeGenerated > ago(90d) and Name startswith "invoke_agent " and isnotempty(tostring(Properties["agw.user.name"]))
| summarize arg_max(TimeGenerated, name = tostring(Properties["agw.user.name"]), upn = tostring(Properties["agw.user.upn"]))
    by user = tostring(Properties["agw.user.id"])
| project user, name, upn;"""

PRICES = """let p_in = todouble("{PriceInput}"); let p_cached = todouble("{PriceCached}"); let p_out = todouble("{PriceOutput}");"""

USER_COST = f"""{NAMES}
{PRICES}
AppDependencies
| where {T} and Name startswith "invoke_agent "
| extend user = tostring(Properties["agw.user.id"]), team = tostring(Properties["agw.team"]),
         cost_center = tostring(Properties["agw.cost_center"]),
         input = toint(Properties["gen_ai.usage.input_tokens"]), output = toint(Properties["gen_ai.usage.output_tokens"]),
         cached = coalesce(toint(Properties["agw.usage.cached_tokens"]), 0)
| summarize runs = count(), input = sum(input), cached = sum(cached), output = sum(output),
            team = take_any(team), cost_center = take_any(cost_center) by user
| join kind=leftouter names on user
| extend who = iff(isempty(name), user, strcat(name, " (", upn, ")")),
         cost_usd = round(((input - cached) * p_in + cached * p_cached + output * p_out) / 1e6, 4)"""


def price_param(n: int, name: str, label: str, value: str) -> dict[str, Any]:
    return {
        **text_param(n, name, label, "USD per 1M tokens: check your Azure price sheet"),
        "value": value,
    }


# ----- Overview ---------------------------------------------------------------------------------


def overview() -> dict[str, Any]:
    tokens = 'Name in ("Prompt Tokens", "Completion Tokens")'
    return notebook(
        text(
            "intro",
            "## Agent gateway\nToken use, limits and agent activity. Per-user rows come from runtime "
            "traces (users are Entra object ids); team, agent and model totals come from APIM token "
            "metrics. See also **Agent gateway: run explorer** and **Agent gateway: tools**.\n\n"
            "Costs are estimates: tokens times the prices below (USD per 1M tokens; defaults are "
            "gpt-5-mini list prices, check your Azure price sheet).",
        ),
        params(
            price_param(7, "PriceInput", "Input $/1M", "0.25"),
            price_param(8, "PriceCached", "Cached input $/1M", "0.025"),
            price_param(9, "PriceOutput", "Output $/1M", "2.00"),
        ),
        query(
            "cost-per-user",
            "Estimated cost per user (USD)",
            USER_COST + "\n| project who, cost_usd\n| order by cost_usd desc\n| take 25",
            "barchart",
            50,
        ),
        query(
            "tokens-by-team",
            "Tokens by team",
            f'AppMetrics | where {T} and {tokens} | summarize tokens = sum(Sum) by team = tostring(Properties["Team"]), bin(TimeGenerated, {{TimeRange:grain}})',
            "timechart",
            50,
        ),
        query(
            "tokens-by-agent",
            "Tokens by agent and model",
            f'AppMetrics | where {T} and {tokens} | summarize tokens = sum(Sum) by agent = tostring(Properties["Agent"]), model = tostring(Properties["Deployment"]) | order by tokens desc',
            "barchart",
            50,
        ),
        query(
            "tokens-by-cost-center",
            "Tokens by cost center (chargeback)",
            f'AppMetrics | where {T} and {tokens} | summarize tokens = sum(Sum) by cost_center = tostring(Properties["CostCenter"]), team = tostring(Properties["Team"]) | order by tokens desc',
            "barchart",
            50,
        ),
        query(
            "token-types",
            "Token types (cached and reasoning tokens affect cost)",
            f'AppMetrics | where {T} and Name has "Tokens" | summarize tokens = sum(Sum) by Name | where tokens > 0 | order by tokens desc',
            width=50,
        ),
        query(
            "blocked",
            "Refused requests: which policy and why",
            f"ApiManagementGatewayLogs | where {T} and ResponseCode in (401, 403, 429) | summarize requests = count() by api = ApiId, status = ResponseCode, reason = LastErrorReason, policy = LastErrorSource | order by requests desc",
            width=50,
        ),
        query(
            "user-cost-table",
            "Cost per user: breakdown",
            USER_COST
            + "\n| project who, team, cost_center, runs, input, cached, output, cost_usd\n| order by cost_usd desc",
            width=50,
        ),
        query(
            "users",
            "Users: runs, tokens and blocks",
            f'{NAMES}\nAppDependencies | where {T} and Name startswith "invoke_agent " | extend user = tostring(Properties["agw.user.id"]), team = tostring(Properties["agw.team"]), cost_center = tostring(Properties["agw.cost_center"]), tier = tostring(Properties["agw.tier"]), agent = tostring(Properties["gen_ai.agent.name"]), stop = tostring(Properties["agw.stop_reason"]), tokens = toint(Properties["gen_ai.usage.input_tokens"]) + toint(Properties["gen_ai.usage.output_tokens"]) | summarize runs = count(), tokens = sum(tokens), blocked = countif(Success == false), budget_stops = countif(stop == "budget_exhausted") by user, team, cost_center, tier, agent | join kind=leftouter names on user | extend who = iff(isempty(name), user, strcat(name, " (", upn, ")")) | project who, team, cost_center, tier, agent, runs, tokens, blocked, budget_stops, user | order by tokens desc',
        ),
        query(
            "limits-hit",
            "Limits hit over time",
            f'AppDependencies | where {T} and Name startswith "chat " and Success == false | summarize hits = count() by limit = tostring(Properties["agw.limit"]), bin(TimeGenerated, {{TimeRange:grain}})',
            "timechart",
            50,
        ),
        query(
            "backend",
            "Model calls, throttling and p95 latency",
            f'ApiManagementGatewayLogs | where {T} and ApiId == "agw-models" and isnotempty(BackendResponseCode) | summarize calls = count(), throttled = countif(BackendResponseCode == 429), p95_ms = percentile(BackendTime, 95) by bin(TimeGenerated, {{TimeRange:grain}})',
            "timechart",
            50,
        ),
    )


# ----- Run explorer -------------------------------------------------------------------------------


def run_explorer() -> dict[str, Any]:
    sessions = f"""{RUNS}
{NAMES}
filtered
| join kind=leftouter inputs on run
| order by TimeGenerated asc
| summarize started = min(TimeGenerated), runs = dcount(run), tokens = sum(tokens), tool_calls = sum(tool_calls),
            blocked = countif(Success == false), first_input = tostring(make_list(input)[0]) by session, user, team
| join kind=leftouter names on user
| project started, who = iff(isempty(name), user, strcat(name, " (", upn, ")")), team, runs, tokens, tool_calls,
          blocked, first_input, session
| order by started desc"""
    runs = f"""{RUNS}
filtered
| where isempty("{{Session}}") or session == "{{Session}}"
| join kind=leftouter inputs on run
| project TimeGenerated, agent, input, stop, tokens, tool_calls, seconds = round(DurationMs / 1000.0, 1),
          ok = Success, run, session, user
| order by TimeGenerated desc"""
    timeline = f"""let r = "{{RunId}}";
let steps = AppDependencies
| where {T} and tostring(Properties["agw.run_id"]) == r and (Name startswith "chat " or Name startswith "execute_tool ")
| extend turn = toint(Properties["agw.turn"]), call_id = tostring(Properties["gen_ai.tool.call.id"]),
         tool = tostring(Properties["gen_ai.tool.name"]), requested = tostring(Properties["agw.tools_requested"]),
         tokens_in = toint(Properties["gen_ai.usage.input_tokens"]), tokens_out = toint(Properties["gen_ai.usage.output_tokens"]),
         outcome = coalesce(tostring(Properties["agw.tool.outcome"]), tostring(Properties["agw.limit"]), iff(Success, "ok", "failed"));
let arguments = AppTraces
| where {T} and Message == "agw.tool_arguments" and tostring(Properties["agw.run_id"]) == r
| project call_id = tostring(Properties["gen_ai.tool.call.id"]), arguments = tostring(Properties["gen_ai.tool.call.arguments"]);
let input = AppTraces
| where {T} and Message == "agw.user_input" and tostring(Properties["agw.run_id"]) == r
| project TimeGenerated, turn = toint(-1), step = "user input", detail = tostring(Properties["agw.input"]), outcome = "", ms = real(null);
union input,
  (steps
   | join kind=leftouter arguments on call_id
   | project TimeGenerated, turn,
             step = iff(Name startswith "chat ",
                        strcat("model → ", iff(isempty(requested), "final answer", strcat("calls ", requested))),
                        strcat("tool ", tool)),
             detail = iff(Name startswith "chat ", strcat(tokens_in, " tokens in / ", tokens_out, " out"), arguments),
             outcome, ms = round(DurationMs))
| order by TimeGenerated asc"""
    upstream = f"""let r = "{{RunId}}";
let op = toscalar(AppDependencies | where {T} and Name startswith "invoke_agent " and tostring(Properties["agw.run_id"]) == r | take 1 | project OperationId);
AppDependencies
| where {T} and OperationId == op and (Name startswith "secsdk " or DependencyType !in ("InProc", ""))
| where not(Name startswith "chat ") and not(Name startswith "execute_tool ") and not(Name startswith "invoke_agent ")
| project TimeGenerated, call = Name, target = Target, status = ResultCode, ok = Success, ms = round(DurationMs),
          cache_hit = tostring(Properties["secsdk.cache_hit"])
| order by TimeGenerated asc"""
    return notebook(
        text(
            "intro",
            "## Run explorer\nPick a **session** (one chat conversation), then a **run** (one question), to see "
            "the user's input and every model call and tool call in order, with tool arguments.\n\n"
            "**Search** matches user input and tool arguments, e.g. `CVE-2024-3400` or a domain. "
            "Inputs and arguments are kept for 30 days; runs without captured content still show "
            "their steps.",
        ),
        params(
            text_param(2, "Search", "Search inputs and tool arguments", "e.g. CVE-2024-3400"),
            text_param(3, "User", "User (object id)", "Entra object id"),
            text_param(4, "Agent", "Agent", "e.g. cve-triage"),
            hidden_param(5, "Session"),
            hidden_param(6, "RunId"),
        ),
        query(
            "sessions",
            "Sessions (select one)",
            sessions,
            exportFieldName="session",
            exportParameterName="Session",
            exportDefaultValue="",
        ),
        query(
            "runs",
            "Runs (select one) — in the selected session, or all matching runs",
            runs,
            exportFieldName="run",
            exportParameterName="RunId",
            exportDefaultValue="",
        ),
        {
            **query("timeline", "Run timeline: input → model calls → tool calls", timeline),
            **visible_when("RunId"),
        },
        {
            **query("upstream", "Upstream API calls made by the tools in this run", upstream),
            **visible_when("RunId"),
        },
    )


# ----- Tool analytics -----------------------------------------------------------------------------


def tools() -> dict[str, Any]:
    tool_spans = f"""AppDependencies
| where {T} and Name startswith "execute_tool "
| extend tool = tostring(Properties["gen_ai.tool.name"]), agent = tostring(Properties["gen_ai.agent.name"]),
         outcome = tostring(Properties["agw.tool.outcome"])"""
    args = f"""AppTraces
| where {T} and Message == "agw.tool_arguments"
| extend tool = tostring(Properties["gen_ai.tool.name"]), arguments = tostring(Properties["gen_ai.tool.call.arguments"]),
         user = tostring(Properties["agw.user.id"]), session = tostring(Properties["gen_ai.conversation.id"]),
         agent = tostring(Properties["gen_ai.agent.name"]), run = tostring(Properties["agw.run_id"])"""
    return notebook(
        text(
            "intro",
            "## Tools\nHow agents use secsdk tools: volume, failures, latency, what they ask for, and "
            "which upstream APIs are slow. Argument views need content capture (30-day retention).",
        ),
        params(
            text_param(2, "Search", "Search tool arguments", "e.g. CVE-2024-3400 or example.com")
        ),
        query(
            "search",
            "Tool calls whose arguments match the search",
            f"""{args}
| where isnotempty("{{Search}}") and arguments contains "{{Search}}"
| project TimeGenerated, tool, arguments, agent, user, session, run
| order by TimeGenerated desc""",
            **{"noDataMessage": "Type a value in Search (for example a CVE id)."},
        ),
        query(
            "calls-over-time",
            "Tool calls over time",
            f"{tool_spans} | summarize calls = count() by tool, bin(TimeGenerated, {{TimeRange:grain}})",
            "timechart",
            50,
        ),
        query(
            "per-tool",
            "Per tool: volume, failures and latency",
            f"""{tool_spans}
| summarize calls = count(), failed = countif(isnotempty(outcome) and outcome != "ok"), p50_ms = round(percentile(DurationMs, 50)),
            p95_ms = round(percentile(DurationMs, 95)), agents = make_set(agent) by tool
| extend failure_pct = round(100.0 * failed / calls, 1)
| order by calls desc""",
            width=50,
        ),
        query(
            "errors",
            "Failures by tool and error code",
            f'{tool_spans} | where isnotempty(outcome) and outcome != "ok" | summarize failures = count() by tool, outcome | order by failures desc',
            width=50,
        ),
        query(
            "cves",
            "Most-queried CVEs",
            f"""{args}
| extend cve = extract_all(@"(CVE-\\d{{4}}-\\d{{4,7}})", toupper(arguments))
| mv-expand cve to typeof(string)
| where isnotempty(cve)
| summarize lookups = count(), users = dcount(user), tools = make_set(tool), last_seen = max(TimeGenerated) by cve
| order by lookups desc
| take 50""",
            width=50,
        ),
        query(
            "top-arguments",
            "Top argument values per tool",
            f"""{args}
| extend bag = parse_json(arguments)
| mv-expand kind = array kv = bag
| extend param = tostring(kv[0]), value = tostring(kv[1])
| where value != "[redacted]"
| summarize calls = count() by tool, param, value
| order by calls desc
| take 100""",
            width=50,
        ),
        query(
            "upstream",
            "Upstream APIs called by tools (secsdk HTTP)",
            f"""AppDependencies
| where {T} and AppRoleName == "{RUNTIME}" and DependencyType !in ("InProc", "") and Name !startswith "POST "
| summarize calls = count(), errors = countif(Success == false), p95_ms = round(percentile(DurationMs, 95)),
            retries = sum(toint(Properties["http.request.resend_count"])) by host = Target
| order by calls desc""",
        ),
    )


WORKBOOKS = {"overview.json": overview, "run-explorer.json": run_explorer, "tools.json": tools}


def main(argv: list[str]) -> int:
    stale = []
    for name, build in WORKBOOKS.items():
        text_out = json.dumps(build(), indent=2) + "\n"
        path = HERE / name
        if "--check" in argv:
            if not path.exists() or path.read_text() != text_out:
                stale.append(name)
        else:
            path.write_text(text_out)
            print(f"wrote {path}")
    if stale:
        print("Stale workbooks (re-run build.py): " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
