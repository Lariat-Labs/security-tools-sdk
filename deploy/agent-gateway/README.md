# Agent gateway (APIM)

Agents as a service behind Azure API Management, with token limits per user, team,
agent and model deployment, all driven by Entra groups. The design, trade-offs and deploy
steps are in [docs/agent-gateway.md](../../docs/agent-gateway.md).

| Path | What |
|---|---|
| `gateway.example.yaml` | Tiers, teams, agents, deployments and their limits |
| `render_policies.py` | Validates the config and writes `policies/*.xml` |
| `policies/` | Rendered APIM policies for the example config (front door, model route) |
| `terraform/` | Everything in Azure and Entra: APIM Standard v2, Foundry/Azure OpenAI, Event Hubs, Container Apps, monitoring, app registrations |
| `infra/workbooks/` | Three Azure Workbooks (overview, run explorer, tools): `build.py` generates them, `validate.py` runs every query live |
| `runtime/` | The agent runtime (FastAPI + secsdk) and its Dockerfile |
| `chat/` | Web chat: MSAL.js sign-in page + backend that calls the gateway on-behalf-of the user |
| `teams/` | Teams bot (Microsoft 365 Agents SDK): SSO + OBO, no secrets; `package.py` builds the Teams app zip |
| `runtime/agw_runtime/a2a.py` | Agent2Agent (A2A) endpoint and agent card, for Copilot Studio |
| `register_agents.py` | Registers the agents as Foundry external agents (trace view) |
| `client.py` | Call the gateway as yourself (`az login`) |
| `tests/` | Offline tests: `uv run pytest deploy/agent-gateway` |

Status: proposal, validated end to end in a pilot deployment (see *Pilot results* in the doc).
Put environment-specific configs and rendered policies in `build/` (git-ignored). Releases:
`.github/workflows/agent-gateway-release.yml` (see `terraform/README.md`).
