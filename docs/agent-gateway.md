# Agent gateway on Azure: proposal

!!! abstract "Status: proposal, validated in a pilot (24 October 2026)"
    This page is the architecture and design proposal for running secsdk-powered agents
    **as a service** behind Azure API Management (APIM). It covers per-user and per-group
    token limits, entitlements and visibility.

    The implementation is in [`deploy/agent-gateway/`](https://github.com/Lariat-Labs/security-tools-sdk/tree/main/deploy/agent-gateway):
    a policy generator, Bicep, an agent runtime, a dashboard and Entra setup. It was
    deployed end to end to a pilot subscription; see [Pilot results](#pilot-results).

## Summary

Users in Microsoft Entra ID call agents through one APIM gateway. The gateway knows who
each user is, which **tier** and **team** they belong to, and which **agents** they may
use, all from their Entra group memberships. Every model call an agent makes goes back
through the same gateway, which enforces these limits:

- **Per user:** tokens per minute, per day and per month, set by the user's tier.
- **Per team:** a shared monthly pool, which is also the unit for chargeback.
- **Per agent:** tokens per minute and per day, plus a hard ceiling per run.
- **Per model deployment:** a gateway ceiling kept inside the quota Foundry granted you.

The agent loop runs in our own runtime (Container Apps), not in Foundry Agent Service.
That's the only way to make APIM see the end user on every model call (see [why](#why-not-foundrys-built-in-options)).

## Requirements

These came out of the discovery questions:

| Topic | Decision |
|---|---|
| Consumers | Internal users, one Entra tenant |
| Agent runtime | Own runtime (Container Apps), secsdk in-process |
| Models | Azure OpenAI (GPT) deployments in Foundry, Chat Completions API |
| Limit levels | Per user, per group/team, per agent, per model deployment |
| Windows | Tokens per minute (burst), daily, monthly, and a per-run budget |
| Over the limit | Hard block: `429`/`403` with a retry time and remaining-quota headers |
| Quota assignment | Tiers by Entra group (`sg-agents-standard`, `sg-agents-power`, …) |
| Agent access | One Entra group per agent (`sg-agent-cve-triage`, …) |
| Clients | Teams / M365 Copilot (bot SSO + on-behalf-of) and API / SDK / CLI |
| Visibility | Usage dashboards, cost chargeback, audit trail, alerts; session and tool-call explorer |
| Captured content | User input and tool arguments (sensitive arguments redacted), kept 30 days; no model output or tool results |
| Chargeback unit | Numeric cost center per team group, stamped on every request |
| Network | APIM Standard v2, public endpoint with Entra authentication |

## Why not Foundry's built-in options

Three Foundry features look like they solve this. None of them can enforce limits per end user.

| Option | What it limits | Why it isn't enough |
|---|---|---|
| **Foundry AI Gateway** (Foundry Control Plane → AI Gateway) | TPM and token quota **per project and deployment** | No per-user or per-group limits. Also, Agent Service model calls **bypass** the project's gateway by design (Microsoft Q&A, linked below) |
| **Bring your own model** (agent → APIM connection) | Whatever APIM policy you write | APIM sees the **Foundry project's** identity or key, never the end user. Chat Completions only, prompt agents only |
| **Agent Applications** (published agents) | Who may invoke (Azure RBAC on the application) | No token limits. Stateless Responses only (legacy publishing model) |

APIM's `llm-token-limit` policy does what we need. Its counter key can be any expression,
such as a user's object id from their token, and it supports a per-minute rate, a
quota per hour/day/week/month/year, or both. For it to see the user on **every** model
call, the agent loop has to run somewhere that forwards the user's context. That's our
runtime.

## Architecture

```mermaid
flowchart LR
    subgraph Clients
        T[Teams / M365 Copilot<br/>bot: SSO + on-behalf-of]
        C[API · SDK · CLI<br/>MSAL / az login]
    end
    subgraph APIM["API Management (Standard v2)"]
        FD["/agents — front door<br/>validate user token · tier/team/entitlement<br/>runs per minute"]
        MR["/models/openai/v1 — model route<br/>runtime identity only · token limits<br/>metrics · usage events"]
    end
    subgraph CA[Container Apps]
        RT["Agent runtime<br/>agent loop · per-run budget<br/>secsdk tools in-process"]
    end
    AOAI[(Azure OpenAI deployments<br/>in Foundry)]
    APIs[(KEV · EPSS · NVD · feeds · OSINT …)]
    EH[[Event Hubs<br/>usage events]] --> LAKE[(Data lake / Fabric / ADX<br/>chargeback)]
    AI[(App Insights + Log Analytics<br/>metrics · logs · traces)]

    T --> FD
    C --> FD
    FD -- "APIM identity + x-agw-* context" --> RT
    RT -- "runtime identity + x-agw-* context" --> MR
    MR -- "APIM identity" --> AOAI
    RT --> APIs
    MR -. log-to-eventhub .-> EH
    MR -. token metrics .-> AI
    RT -. OpenTelemetry .-> AI
```

| Component | Role |
|---|---|
| **APIM `/agents` (front door)** | The only public entry point. Validates the user's Entra token (`Agents.Invoke` scope). Works out tier, team and allowed agents from the `groups` claim. Refuses users with no tier or no entitlement. Limits runs per minute. Overwrites the `x-agw-*` context headers and calls the runtime with APIM's own identity |
| **Agent runtime** | Stateless FastAPI service. Loads agent definitions (instructions, model, allowed secsdk providers, run budget). Runs the Chat Completions tool loop with secsdk operations as function tools, and sends the user context with every model call |
| **APIM `/models` (model route)** | The only route to the models. Accepts only the runtime's managed identity (`Gateway.Internal` app role). Checks the agent may use the requested model, applies every token limit, emits metrics, writes one usage event per call, then calls Azure OpenAI with APIM's identity |
| **Azure OpenAI in Foundry** | Model deployments with key auth disabled. Only APIM's identity has *Cognitive Services OpenAI User* |

Why two APIM APIs instead of one: users must never reach the models directly. The model
route trusts the `x-agw-*` headers only because just one caller can reach it: the
runtime's identity, checked by token. If users could call the model route, they could
claim any team or tier.

## Request flow

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant G as APIM /agents
    participant R as Agent runtime
    participant M as APIM /models
    participant O as Azure OpenAI
    U->>G: POST /agents/cve-triage/runs<br/>Bearer (user token, scp=Agents.Invoke)
    G->>G: validate token · groups → tier=standard, team=secops,<br/>allowed=[cve-triage] · runs/min
    G->>R: x-agw-user/tier/team/run/allowed-agents<br/>Bearer (APIM managed identity)
    loop each turn (≤ max_turns, ≤ budget)
        R->>M: chat/completions + x-agw-* (Bearer runtime identity)
        M->>M: model allowed? · deployment TPM · agent TPM/day · run quota<br/>user TPM/day · user month · team month
        alt within limits
            M->>O: chat/completions (APIM identity)
            O-->>M: completion + usage
            M-->>R: completion + remaining-quota headers
            M--)M: token metrics + usage event
            R->>R: run secsdk tools (KEV, EPSS, …)
        else a limit is hit
            M-->>R: 429 (rate) or 403 (quota) + x-agw-retry-after-<limit>
            R-->>U: same status, Retry-After, which limit
        end
    end
    R-->>U: output · usage · tool calls · remaining limits
```

## Identity and access

### The app registration

One Entra app registration, **agent-gateway**, is the audience for everything.
[`entra.sh app`](https://github.com/Lariat-Labs/security-tools-sdk/blob/main/deploy/agent-gateway/entra.sh) creates it:

| Setting | Value | Why |
|---|---|---|
| Identifier URI | `api://<app-id>` | Entra's default policy requires this format for new apps |
| Delegated scope | `Agents.Invoke` | Users' and clients' tokens must carry it (front door) |
| App role `Agents.User` | assigned to every tier, team and agent group | With *assignment required*, only members can get a token at all |
| App role `Gateway.Internal` | assigned to the APIM and runtime managed identities | The model route and the runtime accept only these |
| `groupMembershipClaims` | `ApplicationGroup` | Tokens list only groups assigned to this app: small tokens, no [group overage](https://learn.microsoft.com/entra/identity-platform/id-token-claims-reference#groups-overage-claim), no unrelated groups leaked |
| Access token version | 2 | |
| Pre-authorised client | Azure CLI | `az login` users can get a token without a consent prompt |

### Groups

| Kind | Example | Decides |
|---|---|---|
| Tier | `sg-agents-standard`, `sg-agents-power` | Personal TPM, daily and monthly limits. A user's tier is the **first** match in `gateway.yaml`, so list the most generous first |
| Team | `sg-team-secops`, `sg-team-itops` | Shared monthly pool and chargeback unit. Users in no team go to `default_team` |
| Agent | `sg-agent-cve-triage` | Who may run that agent |

Use **direct** membership for these groups and assign each one to the app (`entra.sh group`).
Assigning groups to an application requires Entra ID P1. For self-service and approvals,
put the groups in Entra **access packages** (Entra ID Governance) with expiry and reviews.

Group changes take effect when the user's next token is issued (typically within an hour).

### Service-to-service trust

| Caller → callee | How the callee checks it |
|---|---|
| APIM → runtime | Container Apps built-in auth: audience `api://<app-id>`, `allowedApplications` = APIM's user-assigned identity. Anything else gets `401` before the code runs |
| Runtime → APIM model route | `validate-azure-ad-token`: `client-application-ids` = runtime identity and `roles` contains `Gateway.Internal` |
| APIM → Azure OpenAI | Managed identity with *Cognitive Services OpenAI User*; the Foundry resource has local (key) auth disabled |

### Clients

**API, SDK and CLI.** Any client that can get a delegated token for
`api://<app-id>/Agents.Invoke`. For people, use `az login` (pre-authorised) or MSAL in
a public client app. For scripts acting as a person, use the same flows; there are no
API keys. [`client.py`](https://github.com/Lariat-Labs/security-tools-sdk/blob/main/deploy/agent-gateway/client.py) is a minimal example:

```bash
az login
export AGW_GATEWAY_URL=https://<apim>.azure-api.net AGW_GATEWAY_APP_ID=<app-id>
python deploy/agent-gateway/client.py list
python deploy/agent-gateway/client.py run cve-triage "Prioritise CVE-2024-3400 and CVE-2023-4966"
```

**Web chat (on-behalf-of).** [`chat/`](https://github.com/Lariat-Labs/security-tools-sdk/tree/main/deploy/agent-gateway/chat)
is a sign-in page plus a small backend, deployed next to the runtime. It is the same
middle-tier pattern a Teams bot uses, so it tests OBO without a Teams licence:

```mermaid
sequenceDiagram
    autonumber
    actor U as User (browser)
    participant E as Microsoft Entra ID
    participant C as Chat backend
    participant G as APIM /agents
    U->>E: sign in (MSAL.js, redirect) for api://<chat-app>/access_as_user
    E-->>U: token, aud = chat app
    U->>C: POST /api/chat (Bearer chat token)
    C->>C: validate signature, issuer, audience, scope
    C->>E: on-behalf-of: user token + client assertion<br/>(chat managed identity, federated credential)
    E-->>C: token, aud = gateway, scp = Agents.Invoke,<br/>same user (oid), groups
    C->>G: POST /agents/{agent}/runs (Bearer OBO token)
    G-->>C: answer, usage, remaining limits (or 429/403)
    C-->>U: answer
```

- **No client secret.** The chat app registration trusts the backend's managed identity
  through a federated identity credential; the managed identity's token for
  `api://AzureADTokenExchange` is the client assertion in the OBO request.
- **Consent is granted tenant-wide** for `Agents.Invoke` and `access_as_user` (OBO can't
  show a consent prompt).
- The side panel shows the claims of both tokens (never the tokens): `aud` changes from
  the chat app to the gateway, `azp` becomes the chat app, `oid` stays the same and the
  gateway token carries the user's groups. That is OBO working.
- The page keeps the conversation and sends earlier turns as `history`; the backend and
  runtime keep no state.

**Teams.** [`teams/`](https://github.com/Lariat-Labs/security-tools-sdk/tree/main/deploy/agent-gateway/teams)
is a bot on the Microsoft 365 Agents SDK (Python, 1.8). It is the same middle tier as the web
chat, with Teams SSO in front:

```mermaid
sequenceDiagram
    autonumber
    actor U as User in Teams
    participant T as Teams + Azure Bot
    participant B as Bot (Container Apps)
    participant E as Microsoft Entra ID
    participant G as APIM /agents
    U->>T: message
    T->>B: activity (Bot Service token)
    B->>T: need a token (OAuth connection "teams-sso")
    T->>E: Teams SSO, silent: token for api://botid-<app>
    T-->>B: user token (via the Bot token store)
    B->>E: on-behalf-of → api://<gateway>/.default
    E-->>B: gateway token as the user
    B-->>U: "Working on it…"
    B->>G: POST /agents/{agent}/runs (OBO token, x-agw-session)
    G-->>B: answer, usage
    B-->>U: answer (proactive message)
```

- **No secrets anywhere.** One app registration is both the bot's identity and its SSO
  app (`api://botid-<app-id>`, scope `defaultScopes`, Teams clients pre-authorised). It has
  two federated identity credentials:
  - one for the **Azure Bot token store**, so the OAuth connection uses the *AAD v2 with
    Federated Credentials* provider;
  - one for the **bot's managed identity**, so the bot authenticates to Bot Service and
    performs OBO with `AuthType=FederatedCredentials`.
- **Long runs.** The bot replies "Working on it…" at once and posts the answer later as a
  proactive message, so Teams never waits on the agent.
- **Commands:** `help`, `agents`, `use <agent>`, `me` (tier, team, cost center, limits),
  `new`, `signout`. Anything else is a question for the current agent.
- **State.** Conversation history and the chosen agent live in memory, one replica. For
  more replicas, store them in Cosmos DB keyed by conversation id. Each conversation has a
  session id (`teams-…`) for the run explorer.
- **App package.** `teams/package.py` builds the manifest (with `webApplicationInfo` for SSO)
  and icons into a zip to upload in Teams or the Teams admin center.

**Your gateway profile: `GET /agents/me`.** APIM answers this itself, from the values it
just derived from the token (the runtime isn't called): user, tier and its token limits,
team, cost center, team pool and allowed agents. Tier, team and cost center are not in
the token: they come from your groups and `gateway.yaml`. The web chat shows it in its
side panel and Teams answers `me` with it.

## Limits

### What is enforced where

Every model call must pass **all** of these. They're evaluated in order, and the first
limit hit is the one reported.

| Level | Counter key | Window | Exceeded | Header naming the limit |
|---|---|---|---|---|
| Runs started (front door) | `runs:<oid>` | per minute | `429` | `Retry-After` |
| Deployment | `d:<deployment>` | TPM | `429` | `x-agw-retry-after-deployment` |
| Agent | `a:<agent>` | TPM + daily quota | `429` / `403` | `x-agw-retry-after-agent` |
| Run | `r:<run-id>` | quota (hard backstop) | `403` | `x-agw-retry-after-run` |
| User | `u:<oid>:<tier>` | TPM + daily quota | `429` / `403` | `x-agw-retry-after-user` |
| User | `u:<oid>:month` | monthly quota | `403` | `x-agw-retry-after-user-month` |
| Team | `t:<team>` | monthly quota | `403` | `x-agw-retry-after-team` |

Successful responses carry `x-agw-*-remaining` headers (user minute, day and month; team
month; agent day; run). The runtime returns them as `limits`. They are **estimates** that
become accurate only near a limit (in the pilot, a counter 5K tokens into a 500K month
reported 495K left while 80K had really been used). Show them as "about N left" and use
the usage events for exact figures.

### Per-run budget: two layers

1. **Runtime (soft).** Each agent definition has `budget_tokens` and `max_turns`. Before each
   model call, the runtime estimates the next prompt size and stops cleanly with
   `stop_reason: "budget_exhausted"` instead of starting a call that would overrun.
2. **Gateway (hard).** `tokens_per_run` in `gateway.yaml` is a quota on the `r:<run-id>`
   counter. It catches a runtime bug or a compromised runtime. A test enforces
   `budget_tokens ≤ tokens_per_run`.

### How the counting behaves

- **Counts are actual usage.** `estimate-prompt-tokens="false"`, so the limits use the
  `usage` block from each model response. The runtime never streams (the gateway refuses
  `stream: true`), because APIM estimates tokens for streamed calls.
- **Per-minute limits are a token bucket that can be overdrawn.** A call is let through
  whenever any allowance is left and charged when its response arrives, so one large call
  can take the bucket below zero; the next calls then get `429` until it refills. In the
  pilot, one run used 11.5K tokens within a minute against a 5K limit, and the next two
  runs were refused with `429` and retry times of about 20 seconds.
- **Concurrent bursts overshoot more.** In the pilot, 5 runs started together pushed 61K
  tokens through a 30K-per-minute limit in about 40 seconds without a single `429`, and
  the daily counter lagged by several thousand tokens before blocking. Treat per-minute
  limits as protection against sustained abuse and runaway loops, not as an exact meter;
  daily and monthly quotas did enforce (`403` with the time until the UTC reset).
- **Quota windows are fixed and UTC.** Daily resets at 00:00 UTC; monthly on the 1st.
  A run that crosses midnight UTC starts a fresh run counter; the runtime budget still applies.
- **Counters are per gateway region.** In a multi-region APIM they aren't aggregated.
  Start single-region, or divide limits per region.
- **Tier changes.** The per-minute and daily counter includes the tier, because APIM v2
  needs a consistent `tokens-per-minute` per key. Moving a user to another tier starts a
  fresh day. The monthly counter doesn't include the tier, so upgrading doesn't reset the month.
- **Only prompt and completion tokens are counted.** Cached prompt tokens count in full
  against limits. Usage events record `cachedTokens` separately for cost.

### Capacity planning against Foundry quota

Foundry grants each deployment a TPM quota per region (GlobalStandard `gpt-5-mini` in
eastus2 is 1,000K TPM on the test subscription). The gateway shares it out:

```text
deployment ceiling  (gateway, d:)  ≤  deployment capacity (Bicep)  ≤  regional quota
   400K TPM                             500K TPM                        1,000K TPM
```

- Keep the **gateway ceiling** below the deployment's capacity so the gateway returns a
  clear `429` before Azure OpenAI throttles everyone at once.
- **Overbook user TPM.** Sum of active users × tier TPM can exceed the ceiling, because
  few users peak together. Watch the deployment `429` rate and raise capacity (or add a
  second deployment behind a backend pool) when it climbs.
- **Monthly team pools** are a budget, not capacity: set them from cost targets.
- For predictable heavy use, add **provisioned throughput (PTU)** as the primary backend
  and spill over to standard deployments with an APIM backend pool and circuit breaker.
  The Bicep already sets a circuit breaker on the models backend.

## Configuration

Limits and entitlements are configuration, not code.
[`gateway.example.yaml`](https://github.com/Lariat-Labs/security-tools-sdk/blob/main/deploy/agent-gateway/gateway.example.yaml):

```yaml
runs_per_minute: 10
tiers:            # first match wins: most generous first
  - {name: power,    group_id: <guid>, tokens_per_minute: 60000, tokens_per_day: 1500000, tokens_per_month: 20000000}
  - {name: standard, group_id: <guid>, tokens_per_minute: 20000, tokens_per_day:  300000, tokens_per_month:  4000000}
teams:
  - {name: secops, group_id: <guid>, tokens_per_month: 60000000, cost_center: CC-1001}
default_team: {name: unassigned, tokens_per_month: 5000000}
agents:
  - {name: cve-triage, group_id: <guid>, deployments: [gpt-5-mini],
     tokens_per_minute: 150000, tokens_per_day: 10000000, tokens_per_run: 300000}
deployments:
  - {name: gpt-5-mini, tokens_per_minute: 400000}
```

`render_policies.py` validates the file and writes the two APIM policies:

```bash
uv run python deploy/agent-gateway/render_policies.py my-gateway.yaml
uv run python deploy/agent-gateway/render_policies.py my-gateway.yaml --check   # in CI
```

Every team has a numeric **cost center**. The front door stamps it on the request (to the
runtime and its traces); the model route derives it from the team itself (it doesn't trust
a header) and adds it to every usage event and as a metric dimension, so chargeback can
group by `costCenter` directly. `default_team` may have one for unassigned usage.

Limits are written into the policy XML as literals because APIM's `tokens-per-minute`
attribute doesn't accept expressions. Names are restricted to `a-z0-9-` because they end
up inside C# policy expressions. Environment-specific ids (tenant, app, managed
identities) stay as APIM named values (`{{agw-…}}`) that Bicep creates.

Agent behaviour lives next to the runtime, one YAML file per agent
([`runtime/agw_runtime/agents/`](https://github.com/Lariat-Labs/security-tools-sdk/tree/main/deploy/agent-gateway/runtime/agw_runtime/agents)):
instructions, model deployment, the secsdk **providers** it may call, `max_turns`,
`budget_tokens`. The platform team owns `gateway.yaml`; agent authors own the agent
files. A test checks they agree: same agents, an allowed deployment, budget within the run limit.

## Visibility

Four streams, each with a job, plus a dashboard on top:

| Stream | Where | Granularity | Use it for |
|---|---|---|---|
| **Token metrics** (`llm-emit-token-metric`, namespace `agent-gateway`) | App Insights custom metrics | Tier, Team, Agent, Deployment | Dashboards and metric alerts |
| **Usage events** (`log-to-eventhub`, one per model call) | Event Hubs → data lake / Fabric / ADX | Per call: user, team, tier, agent, run, deployment, prompt/completion/cached tokens, status | Chargeback and the authoritative per-user record |
| **Gateway and LLM logs** | Log Analytics (`ApiManagementGatewayLogs`, `ApiManagementGatewayLlmLog`) | Per request: status, latency, backend, token counts | Troubleshooting, `429`/`403` rates, cross-checks |
| **Runtime traces** (OpenTelemetry via secsdk telemetry) | App Insights | Per run: user, agent, each model call, each tool call and outcome | Audit trail, per-user views, debugging agents |

### Dashboards

Bicep deploys three Azure Workbooks (Azure portal → Monitor → Workbooks, or the resource
group). They are generated by [`infra/workbooks/build.py`](https://github.com/Lariat-Labs/security-tools-sdk/blob/main/deploy/agent-gateway/infra/workbooks/build.py),
and `infra/workbooks/validate.py` runs every query against a live workspace before deploy.

| Workbook | What it answers |
|---|---|
| **Agent gateway** | Tokens by team, agent, model and cost center; estimated **cost per user** (bar chart and breakdown; prices are parameters, defaulting to gpt-5-mini list prices); token types (cached, reasoning); refused requests and the policy that refused them; users with names; limits hit; model throttling and latency |
| **Agent gateway: run explorer** | Pick a **session** (one conversation), then a **run**, to see the user's question, every model call (tokens, which tools it asked for) and every tool call with its **arguments**, outcome and timing, plus the upstream API calls the tools made. **Search** finds runs by input or tool argument, e.g. a CVE id or a domain |
| **Agent gateway: tools** | Per tool: volume, failures, p50/p95 latency; failures by error code; most-queried CVEs; top argument values; slow or failing upstream APIs; search tool calls by argument |

APIM's own **Monitoring → Analytics → Language models** tab adds token charts from the LLM logs.
The runtime's spans follow the OpenTelemetry GenAI conventions (`invoke_agent`, `chat`,
`execute_tool`, `gen_ai.conversation.id`, `gen_ai.agent.id`), so App Insights' transaction
view shows each run as a tree too.

**Foundry trace view.** Bicep creates a Foundry project with this Application Insights
connected, and `register_agents.py` registers each agent as an **external agent** (preview).
Foundry matches spans by `gen_ai.agent.id` (the agent name), so each agent's **Traces** tab in
the Foundry portal shows its runs. Captured content lives in span events (below), so the
Foundry view shows structure, tokens and latency rather than inputs.

### Sessions and captured content

- **Session.** Clients send `x-agw-session` (the web chat makes one per conversation, the
  Teams bot one per Teams conversation). The front door keeps it if it matches
  `^[A-Za-z0-9-]{1,64}$`, otherwise uses the request id. It is on every span
  (`gen_ai.conversation.id`, `session.id`) and every usage event (`sessionId`).
- **Content capture** (`AGW_CAPTURE_CONTENT=true`, on in the Bicep): the runtime records the
  user's input and each tool call's arguments as **span events**. Azure Monitor stores span
  events in `AppTraces`, which Bicep sets to **30 days**; spans stay at 90. Model answers and
  tool results are never recorded. Arguments listed in `AGW_REDACT_ARGS` (default
  `azure.query.query`, the Resource Graph KQL) are replaced with `[redacted]`.
- **Names.** The front door passes the user's display name and sign-in name from the token;
  runs record them (`agw.user.name`, `agw.user.upn`) so dashboards show people, not object ids.
- **Sampling.** `azure-monitor-opentelemetry` defaults to a rate-limited sampler (5 traces
  per second) that dropped spans in the middle of runs, leaving holes in timelines. The
  runtime, chat and bot set `OTEL_TRACES_SAMPLER=always_on`. At high volume, weigh the
  ingestion cost against a complete audit trail.

!!! warning "Why per-user data isn't in metrics"
    Azure Monitor custom metrics allow at most **5 custom dimensions**, and APIM tracks at
    most **100 values per dimension**; values beyond that are silently dropped. A user id
    dimension would break that. Per-user numbers come from usage events and runtime traces,
    which are logs and have no cardinality limit.

### Privacy

- **Model output is never stored.** Bicep turns on APIM's LLM logs (token counts per call)
  but not the separate *Log prompts* / *Log completions* options. The runtime's spans
  record operation names, outcomes and token counts; user input and tool arguments are
  span events with 30-day retention (above). secsdk's own spans record parameter **names**
  only, unless `SECSDK_TELEMETRY_CAPTURE_PARAMS=true`.
- **People** are identified by Entra object id everywhere, and by display and sign-in
  name on runs (for dashboards). Turn names off by removing the two `x-agw-user-name` /
  `x-agw-user-upn` headers from the front-door policy.
- Usage events and traces are personal data under most policies. Set Log Analytics and
  lake retention to match your policy, and restrict access to the usage table.

### Queries

Tokens by team and agent (metrics), last 7 days. APIM emits `Prompt Tokens` and
`Completion Tokens` (plus breakdowns such as `Prompt Cached Tokens` and
`Completion Reasoning Tokens`); there is no total metric:

```kusto
AppMetrics
| where TimeGenerated > ago(7d) and Name in ("Prompt Tokens", "Completion Tokens")
| extend team = tostring(Properties["Team"]), agent = tostring(Properties["Agent"])
| summarize tokens = sum(Sum) by team, agent, bin(TimeGenerated, 1d)
| render columnchart
```

Per-user activity and blocks (runtime traces):

```kusto
AppDependencies
| where TimeGenerated > ago(7d) and Name startswith "invoke_agent "
| extend user = tostring(Properties["agw.user.id"]), team = tostring(Properties["agw.team"]),
         stop = tostring(Properties["agw.stop_reason"]),
         tokens = toint(Properties["gen_ai.usage.input_tokens"]) + toint(Properties["gen_ai.usage.output_tokens"])
| summarize runs = count(), tokens = sum(tokens), budget_stops = countif(stop == "budget_exhausted"),
            failed = countif(Success == false) by user, team
| order by tokens desc
```

Chargeback from usage events, once they're in a table (for example Fabric Eventhouse or
ADX ingesting the `agw-usage` hub). Prices come from your Azure price sheet; don't hard-code them:

```kusto
let price = datatable(deployment:string, input_per_1m:real, cached_per_1m:real, output_per_1m:real)
    [ "gpt-5-mini", 0.0, 0.0, 0.0 ];   // fill in from your price sheet
AgwUsage
| where time >= startofmonth(now()) and status == 200
| lookup price on deployment
| extend cost = (promptTokens - cachedTokens) * input_per_1m / 1e6
             + cachedTokens * cached_per_1m / 1e6
             + completionTokens * output_per_1m / 1e6
| summarize tokens = sum(totalTokens), cost = round(sum(cost), 2) by team
```

To land usage events in a **data lake** with no extra compute, turn on Event Hubs
**Capture** to an ADLS Gen2 account (Avro files, partitioned by time).

### Alerts

| Alert | Source | Rule |
|---|---|---|
| A team passes 80% of its monthly pool | Usage events | Sum of `totalTokens` this month per team ÷ pool (from `gateway.yaml`) > 0.8 |
| Users blocked by a limit | Gateway logs | `ApiManagementGatewayLogs` with `LastErrorReason` in (`OpenAITokenLimitExceeded`, `OpenAITokenQuotaExceeded`), count in 15 min; runtime `chat` spans name the limit in `agw.limit` |
| Deployment saturation | Gateway logs | Share of `429` from the models backend over 5 min > 5% |
| Runaway agent | Runtime traces | `budget_exhausted` stops for one agent above normal |
| Gateway errors | Gateway logs | `5xx` rate on `/agents` |

Wire them to an action group (email, Teams, or a Logic App that opens a ticket).

## Deploy

These are the steps the pilot used.

```bash
# 1. Entra: app registration, groups (put the ids in your gateway.yaml)
cd deploy/agent-gateway
./entra.sh app                                   # prints gatewayAppId
./entra.sh group <app-id> sg-agents-standard     # repeat for tiers, teams and agents

# 2. Policies from your config, into a git-ignored folder
uv run python render_policies.py build/my-gateway.yaml --out build/policies

# 3. Infrastructure without the runtime (APIM Standard v2 takes a while)
az group create -n rg-agent-gateway -l eastus2
#    agentsPolicyXml/modelsPolicyXml default to the example policies; pass yours, e.g. in a
#    parameters JSON file generated from build/policies/*.xml (the pilot did this)
az deployment group create -g rg-agent-gateway -f infra/main.bicep \
  -p publisherEmail=<platform-team-address> gatewayAppId=<app-id> -p @build/policies.parameters.json

# 4. Let APIM and the runtime call each other (principal ids are deployment outputs)
./entra.sh grant <app-id> <apimPrincipalId> <runtimePrincipalId>

# 5. Build the runtime image from the repo root, then deploy it
cd ../..
az acr build -r <registryName> -t agw-runtime:0.1.0 -f deploy/agent-gateway/runtime/Dockerfile .
#    (or docker build --platform linux/amd64 … && az acr login -n <registryName> && docker push …)
az deployment group create -g rg-agent-gateway -f deploy/agent-gateway/infra/main.bicep \
  -p publisherEmail=<…> gatewayAppId=<app-id> -p @deploy/agent-gateway/build/policies.parameters.json \
  -p runtimeImage=<registryLoginServer>/agw-runtime:0.1.0

# 6. Try it
python client.py list

# 7. Web chat (OBO): app registration, image, deploy, then link the redirect URI and identity
cd deploy/agent-gateway
./entra.sh chat <app-id>                         # prints chatAppId, grants consent
docker build --platform linux/amd64 -t <registryLoginServer>/agw-chat:0.1.0 -f chat/Dockerfile ../..
docker push <registryLoginServer>/agw-chat:0.1.0
az deployment group create … -p chatAppId=<chatAppId> chatImage=<registryLoginServer>/agw-chat:0.1.0
./entra.sh chat-link <chatAppId> <chatUrl> <chatPrincipalId>   # outputs of the deployment

# 8. Teams bot: app registration (trusts the bot identity and the Bot token store), image,
#    deploy, then build the app package and upload it in Teams
./entra.sh teams <app-id> <botPrincipalId>        # prints teamsAppId
docker build --platform linux/amd64 -t <registryLoginServer>/agw-teams:0.2.0 -f teams/Dockerfile ../..
docker push <registryLoginServer>/agw-teams:0.2.0
az deployment group create … -p teamsAppId=<teamsAppId> teamsImage=<registryLoginServer>/agw-teams:0.2.0
uv run python teams/package.py --app-id <teamsAppId> --bot-host <teamsBotEndpoint host>

# 9. Foundry trace view: register the agents as external agents
uv run python register_agents.py --endpoint <foundryProjectEndpoint>
```

The image build context is the repo root, and `.dockerignore` is an allow-list
(`packages/secsdk` and the runtime only), so local files such as `.env` or `.envrc`
never reach the registry.

What the Bicep creates: user-assigned identities for APIM and the runtime, APIM
Standard v2 (named values, App Insights logger with custom metrics, Event Hubs logger,
diagnostic settings to Log Analytics, the two APIs and their policies, a models backend
with a circuit breaker), an AI Services (Foundry) account with key auth disabled and the
model deployments, Event Hubs, Container Registry, a Container Apps environment and the
runtime app with Entra-only ingress auth, the *Agent gateway* workbook, APIM LLM logs
(token counts only), plus the role assignments.

**Terraform instead of Bicep** works too: the `azurerm` provider covers nearly all of these
resources, `azapi` covers the rest (for example the Container Apps auth config and APIM's
LLM logging setting), and `azuread` can replace `entra.sh`. The rendered policy XML is
used the same way.

## Decisions and alternatives

| Decision | Chosen | Alternatives considered |
|---|---|---|
| Where the agent loop runs | Own runtime on Container Apps | **Foundry prompt agents**: less code, but APIM can't see the user on model calls, so only per-run totals could be limited (from the response's `usage`, after the fact). **Foundry hosted agents**: possible later; the runtime would need to forward the user context the same way |
| How the user reaches the model route | Runtime forwards `x-agw-*` headers, trusted because only the runtime identity can call | Forward the user's own token (needs a second credential so users can't call models directly), or have the front door sign a context JWT (stronger against a compromised runtime, more moving parts) |
| Limit enforcement | APIM `llm-token-limit` | Counting in the runtime against Redis/Cosmos: more flexible, but every runtime would have to be trusted to count |
| Quota assignment | Groups → tiers, compiled into policy | Per-user overrides in a lookup service (APIM `send-request` + cache): add when exceptions are common |
| Per-user reporting | Usage events + traces | Custom metric with a user dimension: blocked by the 100-value limit |
| APIM tier | Standard v2 | Basic v2 (no VNet integration); Premium v2 (VNet injection, multi-region) when fully private networking is required |
| Tools | secsdk in-process | The [MCP server](deploy-azure.md) behind APIM (APIM can front an existing MCP server, in preview). Useful when other platforms' agents need the same tools |

## Pilot results

Deployed to a dedicated resource group (eastus2) with low limits on 24 October 2026:
APIM Standard v2, `gpt-5-mini` (GlobalStandard, 500K TPM), the runtime on Container Apps.

| Check | Result |
|---|---|
| APIM accepts the generated policies, including `llm-token-limit` on a plain HTTP API | ✅ deployed without changes |
| Token carries only the app-assigned groups (`ApplicationGroup`) and `scp=Agents.Invoke` | ✅ |
| `GET /agents` lists only entitled agents | ✅ `cve-triage` only |
| End-to-end run (front door → runtime → model route → Azure OpenAI, live secsdk tools) | ✅ 4 model calls, 4 tool calls, ~30 s, all remaining-quota headers returned |
| Agent without entitlement | ✅ `403 not_entitled` |
| No token at the front door | ✅ `401` |
| User token sent to the model route (with forged context headers) | ✅ `401` |
| Runtime called directly, bypassing APIM | ✅ `403` from Container Apps auth |
| Azure OpenAI called directly | ✅ `401` (key auth disabled) |
| Daily quota | ✅ `403 quota_exceeded`, `limit: user`, retry ≈ time to 00:00 UTC |
| Per-minute limit, sequential use | ✅ `429 rate_limited`, retry ≈ 20 s, after one overdraw |
| Per-minute limit, 5 concurrent runs | ⚠️ overshot ~2× without a `429` (see [How the counting behaves](#how-the-counting-behaves)) |
| Remaining-quota headers | ⚠️ estimates; inaccurate far from the limit |
| Token metrics with Tier/Team/Agent/Deployment | ✅ (names are `Prompt Tokens`, `Completion Tokens`, …) |
| Usage events on Event Hubs | ✅ |
| Runtime traces with `agw.user.id`, stop reason, tool calls | ✅ |
| LLM logs with token counts and no message bodies | ✅ after enabling `largeLanguageModel` logs |
| Gateway logs name the refusing policy (`OpenAITokenQuotaExceeded`, `llm-token-limit[1]`) | ✅ |
| Web chat: OBO with a managed-identity federated credential (no secret) | ✅ gateway token has `aud` = gateway, `azp` = chat app, same `oid`, 3 groups |
| Web chat: agent run through chat → OBO → gateway → runtime → model + tools | ✅ 3 model calls, 4 tool calls, ~17 s |
| Session id from the chat through APIM to every span and usage event | ✅ |
| Run explorer: input → model calls → tool calls with arguments | ✅ all 21 workbook queries validated against live data |
| Complete runs (every model-call span present) | ✅ after `always_on` sampling; ❌ before (default sampler dropped spans) |
| Cost center on requests, usage events, metrics and runs | ✅ |
| Cost per user with names and cached-token split | ✅ |
| `GET /agents/me` directly and through chat OBO | ✅ tier, team, cost center, limits, agents |
| Foundry external agents registered; project with App Insights connected | ✅ |
| Teams bot deployed: Azure Bot, Teams channel, federated SSO connection, health | ✅ (Teams sign-in not yet tested: needs a licensed user) |

Fixes found by the pilot (already in the code): the CLI client asks for `/.default`
(the Azure CLI credential rejects named scopes); Bicep takes environment-specific policy
XML as parameters; LLM logs and the workbook are part of the Bicep.

Not yet tested: `403 no_tier` (needs a fresh token after leaving the tier group), team and
agent quotas at their limits, and a 20-run load test.

## Risks and open items

- **Burst overshoot:** per-minute limits overshoot under concurrent load (see the pilot).
  If a hard per-minute ceiling matters, add a request-count `rate-limit-by-key` per user on
  the model route, or cap concurrent runs per user at the front door.
- **Teams sign-in** is deployed but untested end to end: the pilot tenant has no Microsoft 365
  licences yet. The web chat proves the same OBO path.
- **Conversation state** is the client's job (the runtime is stateless). A shared
  conversation store would need per-user isolation.
- **Foundry Agent Service** may later support end-user identity on model calls; if it does,
  prompt agents could run behind the same model route.
- **Cost:** APIM Standard v2 and the Event Hubs namespace are fixed monthly costs; the
  runtime keeps one replica warm. Check current pricing for your region.
- **Multi-region:** counters are per region; see [How the counting behaves](#how-the-counting-behaves).

## Sources

- [llm-token-limit policy](https://learn.microsoft.com/azure/api-management/llm-token-limit-policy) and [llm-emit-token-metric policy](https://learn.microsoft.com/azure/api-management/llm-emit-token-metric-policy)
- [AI gateway capabilities in Azure API Management](https://learn.microsoft.com/azure/api-management/genai-gateway-capabilities)
- [validate-azure-ad-token policy](https://learn.microsoft.com/azure/api-management/validate-azure-ad-token-policy)
- [Log token usage, prompts and completions for LLM APIs](https://learn.microsoft.com/azure/api-management/api-management-howto-llm-logs)
- [Enforce token limits for models (Foundry Control Plane)](https://learn.microsoft.com/azure/foundry/control-plane/how-to-enforce-limits-models)
- [Bring your own model to Foundry Agent Service](https://learn.microsoft.com/azure/foundry/agents/how-to/ai-gateway)
- [Does Agent Service route model calls through AI Gateway? (Microsoft Q&A)](https://learn.microsoft.com/answers/questions/5777255/azure-ai-foundry-does-ai-agent-service-route-model)
- [Agent applications in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/agents/how-to/agent-applications)
- [Expose and govern an existing MCP server with API Management](https://learn.microsoft.com/azure/api-management/expose-existing-mcp-server)
