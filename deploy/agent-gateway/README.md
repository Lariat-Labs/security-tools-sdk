# Agent gateway (APIM)

Agents as a service behind Azure API Management, with token limits per user, team,
agent and model deployment, all driven by Entra groups. The design, trade-offs and deploy
steps are in [docs/agent-gateway.md](../../docs/agent-gateway.md).

| Path | What |
|---|---|
| `gateway.example.yaml` | Tiers, teams, agents, deployments and their limits |
| `render_policies.py` | Validates the config and writes `policies/*.xml` |
| `policies/` | Rendered APIM policies for the example config (front door, model route) |
| `infra/main.bicep` | APIM Standard v2, Foundry/Azure OpenAI, Event Hubs, Container Apps, monitoring |
| `infra/workbook.json` | The *Agent gateway* Azure Workbook (dashboard) deployed by the Bicep |
| `entra.sh` | App registration, groups, role grants for the managed identities |
| `runtime/` | The agent runtime (FastAPI + secsdk) and its Dockerfile |
| `chat/` | Web chat: MSAL.js sign-in page + backend that calls the gateway on-behalf-of the user |
| `client.py` | Call the gateway as yourself (`az login`) |
| `tests/` | Offline tests: `uv run pytest deploy/agent-gateway` |

Status: proposal, validated end to end in a pilot deployment (see *Pilot results* in the doc).
Put environment-specific configs, rendered policies and parameter files in `build/` (git-ignored).
