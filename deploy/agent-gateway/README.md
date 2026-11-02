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
| `client.py` | Call the gateway as yourself (`az login`); `--local` calls a local runtime container |
| `run_local.py` | Run the runtime container on your machine against Foundry, with a short-lived token ([how](../../docs/agent-gateway.md#run-the-runtime-container-locally)) |
| `run_webui.py`, `webui/` | Chat with the agents in Open WebUI on your machine, through the deployed gateway (your real limits) or a local runtime ([how](../../docs/agent-gateway.md#chat-in-open-webui-locally)) |
| `terraform/mcp.tf`, `mcp_call.py` | Proof of concept: secsdk's `kev.lookup` as an MCP tool behind APIM (`enable_mcp_tools`), and a client to call it ([how](../../docs/agent-gateway.md#mcp-tools-proof-of-concept)) |
| `tests/` | Offline tests: `uv run pytest deploy/agent-gateway` |

Status: proposal, validated end to end in a pilot deployment (see *Pilot results* in the doc).
Put environment-specific configs and rendered policies in `build/` (git-ignored). Releases:
`.github/workflows/agent-gateway-release.yml` (see `terraform/README.md`).
