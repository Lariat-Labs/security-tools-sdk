"""Agent definitions: one YAML file per agent in ``agents/``.

Gateway limits (who may use an agent, token quotas) live in gateway.yaml and are
enforced by APIM. These files describe what the agent does: its instructions, the
model deployment, the secsdk providers it may call, and a per-run budget that the
runtime enforces before the gateway's hard per-run limit is reached.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from secsdk import default_registry

AGENTS_DIR = Path(__file__).resolve().parent / "agents"


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
    description: str
    deployment: str = Field(description="Model deployment name, sent as `model`")
    providers: list[str] = Field(min_length=1, description="secsdk providers the agent may call")
    instructions: str
    max_turns: int = Field(default=15, gt=0, le=50)
    budget_tokens: int = Field(
        default=200_000, gt=0, description="Stop the run at this many tokens"
    )
    max_output_tokens: int = Field(default=4_000, gt=0, description="Per model call")
    max_tool_chars: int = Field(default=20_000, gt=0, description="Cap on one tool result")

    def tool_names(self) -> dict[str, str]:
        """Tool name -> operation name, for the providers this agent may use."""
        return {
            op.tool_name: op.fqname
            for op in default_registry().operations()
            if op.provider in self.providers
        }


def load_agents(directory: Path | None = None) -> dict[str, AgentDefinition]:
    directory = directory or Path(os.environ.get("AGW_AGENTS_DIR") or AGENTS_DIR)
    agents: dict[str, AgentDefinition] = {}
    known = set(default_registry().names())
    for path in sorted(directory.glob("*.yaml")):
        agent = AgentDefinition.model_validate(yaml.safe_load(path.read_text()))
        if agent.name != path.stem:
            raise ValueError(f"{path.name}: name '{agent.name}' must match the file name")
        unknown = sorted(set(agent.providers) - known)
        if unknown:
            raise ValueError(f"{path.name}: unknown providers {unknown}")
        agents[agent.name] = agent
    return agents
