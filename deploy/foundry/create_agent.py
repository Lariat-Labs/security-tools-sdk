"""Create (or update) a Foundry prompt agent that uses the secsdk MCP server.

    pip install "azure-ai-projects>=2" azure-identity
    az login
    export FOUNDRY_PROJECT_ENDPOINT="https://<resource>.ai.azure.com/api/projects/<project>"
    export FOUNDRY_MODEL="<your model deployment name>"
    export SECSDK_MCP_URL="https://<function-app>.azurewebsites.net/runtime/webhooks/mcp"
    export SECSDK_MCP_CONNECTION="secsdk-mcp"     # project connection holding the auth
    python deploy/foundry/create_agent.py --ask "Prioritise CVE-2024-3400 and CVE-2023-4966"

Based on Microsoft's "Connect agents to MCP server endpoints" sample (azure-ai-projects,
PromptAgentDefinition + MCPTool). Not exercised in this repository's CI: it needs a
live Foundry project.
"""

from __future__ import annotations

import argparse
import os
import sys

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.identity import DefaultAzureCredential

# Read-only tools exposed by deploy/azure-functions/function_app.py.
TOOLS = [
    "secsdk_guide",
    "secsdk_providers",
    "secsdk_describe",
    "secsdk_call",
    "intel_cve_priority",
    "kev_lookup",
    "osint_profile",
    "feeds_news",
]

INSTRUCTIONS = """\
You are a security analyst agent. Your only source of security facts is the secsdk MCP server.

- At the start of a task call secsdk_guide once, then secsdk_providers. Skip providers with configured=false.
- Use the shortcut tools when they fit (intel_cve_priority, kev_lookup, osint_profile, feeds_news);
  use secsdk_call for everything else, and secsdk_describe before an unfamiliar operation.
- Every result is a JSON envelope. Check `ok`. On failure act on error.code:
  invalid_input -> fix the parameters; not_configured -> tell the user what is missing;
  upstream_error -> retry once; not_found -> a valid answer, not a failure.
- Priorities come from intel_cve_priority. Explain them with its `reasons` field; never re-score.
- Do not state facts you did not read in a tool result. Cite the operation behind each finding.
- All tools are read-only and passive.
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--name", default="secsdk-analyst", help="Agent name")
    ap.add_argument("--ask", help="Optional question to send after creating the agent")
    args = ap.parse_args()

    try:
        endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
        model = os.environ["FOUNDRY_MODEL"]
        server_url = os.environ["SECSDK_MCP_URL"]
    except KeyError as exc:
        print(f"Missing environment variable {exc}; see the docstring.", file=sys.stderr)
        return 2
    connection = os.environ.get("SECSDK_MCP_CONNECTION")  # None = unauthenticated (dev only)

    with AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential()) as project:
        tool = MCPTool(
            server_label="secsdk",
            server_url=server_url,
            allowed_tools=TOOLS,
            # Every tool is read-only, so calls don't need human approval. Use "always"
            # if you later add tools that change anything.
            require_approval="never",
            project_connection_id=connection,
        )
        agent = project.agents.create_version(
            agent_name=args.name,
            definition=PromptAgentDefinition(model=model, instructions=INSTRUCTIONS, tools=[tool]),
        )
        print(f"Agent ready: {agent.name} (version {agent.version})")

        if args.ask:
            with project.get_openai_client(agent_name=agent.name) as openai:
                conversation = openai.conversations.create()
                response = openai.responses.create(conversation=conversation.id, input=args.ask)
                for item in response.output:
                    if item.type == "mcp_call":
                        print(f"  tool: {getattr(item, 'name', '?')}", file=sys.stderr)
                print(response.output_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
