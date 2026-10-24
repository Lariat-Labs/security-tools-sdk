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
| `entra.sh` | App registration, groups, role grants for the managed identities |
| `runtime/` | The agent runtime (FastAPI + secsdk) and its Dockerfile |
| `client.py` | Call the gateway as yourself (`az login`) |
| `tests/` | Offline tests: `uv run pytest deploy/agent-gateway` |

Status: proposal. Unit-tested and the Bicep compiles; not yet deployed.
