"""Register the runtime's agents in Foundry as *external* agents (preview).

    az login
    uv run python deploy/agent-gateway/register_agents.py \
        --endpoint https://<account>.services.ai.azure.com/api/projects/agents \
        --gateway-url https://<apim>.azure-api.net
    uv run python deploy/agent-gateway/register_agents.py --dry-run     # show what would be sent

Foundry stores only a registration record; it doesn't host or call the agents. The
runtime sets ``gen_ai.agent.id`` to the agent name on every span, and Foundry's agent
**Traces** tab shows the spans whose ``gen_ai.agent.id`` matches the registration. The
project must have this deployment's Application Insights connected (Terraform does it).

The registration carries a description and metadata built from each agent's YAML (model
deployment, tools, budgets, how to invoke it, a link to its definition and the commit it
was registered from), so Foundry shows what the agent is next to its traces. The
instructions are not copied: they stay in the repository. Re-run after changing an agent
so the details don't go stale; each run adds a new revision of the registration.

Needs the Foundry User role on the project.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "runtime"))

from agw_runtime.agents import AgentDefinition, load_agents  # noqa: E402 (needs the path above)

REPO_URL = "https://github.com/Lariat-Labs/security-tools-sdk"
AGENTS_PATH = "deploy/agent-gateway/runtime/agw_runtime/agents"
# Foundry agent metadata limits: 16 pairs, keys up to 64 characters, values up to 512.
MAX_KEYS, MAX_KEY, MAX_VALUE, MAX_DESCRIPTION = 16, 64, 512, 512


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def registration_details(
    agent: AgentDefinition,
    *,
    gateway_url: str = "",
    repo_url: str = REPO_URL,
    ref: str = "main",
) -> tuple[str, dict[str, str]]:
    """The description and metadata Foundry stores for one agent (no instructions)."""
    definition = f"{repo_url.rstrip('/')}/blob/{ref}/{AGENTS_PATH}/{agent.name}.yaml"
    # Foundry shows only the description on an external agent's page (512 characters max), so
    # it carries the tools; the system prompt is on every trace (gen_ai.system_instructions).
    tools = ", ".join(sorted(agent.tool_names()))
    description = f"{agent.description} Tools: {tools}. Runs in the agent gateway runtime (model calls via APIM)."
    if len(description) > MAX_DESCRIPTION:
        description = f"{agent.description} Tools: {tools}."
    description = _clip(description, MAX_DESCRIPTION)
    metadata = {
        "hosting": "external: agent gateway runtime (Azure Container Apps)",
        "model_deployment": f"{agent.deployment} (via the APIM model route)",
        "tools": ", ".join(sorted(agent.tool_names())),
        "tool_providers": ", ".join(agent.providers),
        "max_turns": str(agent.max_turns),
        "budget_tokens": str(agent.budget_tokens),
        "max_output_tokens": str(agent.max_output_tokens),
        "invoke": f"POST {gateway_url.rstrip('/')}/agents/{agent.name}/runs"
        if gateway_url
        else f"POST <gateway>/agents/{agent.name}/runs",
        "access": "Entra group per agent, token limits per user/team/agent (gateway.yaml)",
        "definition": definition,
        "source_commit": ref,
        "trace_match": f"gen_ai.agent.id = {agent.name}",
    }
    assert len(metadata) <= MAX_KEYS
    return description, {_clip(k, MAX_KEY): _clip(v, MAX_VALUE) for k, v in metadata.items()}


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "main"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--endpoint", default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT"))
    ap.add_argument(
        "--gateway-url", default=os.environ.get("AGW_GATEWAY_URL", ""), help="APIM gateway URL"
    )
    ap.add_argument("--repo-url", default=REPO_URL, help="where agent definitions are browsable")
    ap.add_argument("--ref", default=None, help="git ref for definition links (default: HEAD)")
    ap.add_argument("--dry-run", action="store_true", help="print the registrations, send nothing")
    args = ap.parse_args(argv)
    ref = args.ref or _git_commit()

    regs = {
        name: registration_details(
            agent, gateway_url=args.gateway_url, repo_url=args.repo_url, ref=ref
        )
        for name, agent in load_agents().items()
    }
    if args.dry_run:
        print(
            json.dumps(
                {n: {"description": d, "metadata": m} for n, (d, m) in regs.items()}, indent=2
            )
        )
        return 0
    if not args.endpoint:
        ap.error("pass --endpoint or set FOUNDRY_PROJECT_ENDPOINT")

    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import ExternalAgentDefinition
    from azure.identity import DefaultAzureCredential

    with AIProjectClient(
        endpoint=args.endpoint, credential=DefaultAzureCredential(), allow_preview=True
    ) as project:
        for name, (description, metadata) in regs.items():
            version = project.agents.create_version(
                agent_name=name,
                description=description,
                metadata=metadata,
                # Matches gen_ai.agent.id on the runtime's spans (the agent name).
                definition=ExternalAgentDefinition(otel_agent_id=name),
            )
            print(
                f"registered {version.name} (otel_agent_id={name}, {len(metadata)} metadata keys)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
