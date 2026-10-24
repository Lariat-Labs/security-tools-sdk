"""Register the runtime's agents in Foundry as *external* agents (preview).

    az login
    uv run python deploy/agent-gateway/register_agents.py \
        --endpoint https://<account>.services.ai.azure.com/api/projects/agents

Foundry stores only a registration record; it doesn't host or call the agents. The
runtime sets ``gen_ai.agent.id`` to the agent name on every span, and Foundry's agent
**Traces** tab shows the spans whose ``gen_ai.agent.id`` matches the registration. The
project must have this deployment's Application Insights connected (the Bicep does it).

Needs the Foundry User role on the project.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "runtime"))

from agw_runtime.agents import load_agents


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--endpoint", default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT"))
    args = ap.parse_args(argv)
    if not args.endpoint:
        ap.error("pass --endpoint or set FOUNDRY_PROJECT_ENDPOINT")

    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import ExternalAgentDefinition
    from azure.identity import DefaultAzureCredential

    with AIProjectClient(
        endpoint=args.endpoint, credential=DefaultAzureCredential(), allow_preview=True
    ) as project:
        for agent in load_agents().values():
            version = project.agents.create_version(
                agent_name=agent.name,
                description=agent.description,
                # Matches gen_ai.agent.id on the runtime's spans (the agent name).
                definition=ExternalAgentDefinition(otel_agent_id=agent.name),
            )
            print(f"registered {version.name} (otel_agent_id={agent.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
