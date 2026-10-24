# security-tools-sdk

A structured Python **SDK** (`secsdk`) and **CLI** (`secctl`) for security data:
vulnerability intelligence, passive OSINT, SEC filings, threat news, Tenable and Azure.
An AI agent can drive it, and it is fully usable without one.

```bash
uv sync
uv run secctl guide
uv run secctl intel cve-priority CVE-2024-3400 CVE-2023-4966 -f table
```

| Provider | Source | Key needed? |
|---|---|---|
| `nvd` | NIST NVD CVE API | optional |
| `kev` | CISA Known Exploited Vulnerabilities | no |
| `epss` | FIRST EPSS | no |
| `osint` | crt.sh, DNS-over-HTTPS, RDAP, Shodan InternetDB | no |
| `feeds` | RSS/Atom security news (CISA, Krebs, SANS ISC, …) | no |
| `sec` | SEC EDGAR: filings, 8-K Item 1.05, 10-K Item 1C | contact User-Agent only |
| `feedly` | Feedly Cloud API | token (search is keyless) |
| `tenable` | Tenable Vulnerability Management | API keys |
| `azure` | Azure Resource Graph + security presets | token / SP / `az login` |
| `intel` | Cross-source rules: CVE priority, KEV exposure | no (exposure needs Tenable) |

## Design

```
agent ──shell──▶ secctl (JSON envelope, exit codes) ──▶ secsdk (providers, validation, HTTP) ──▶ APIs
```

- **SDK first.** Fetching, parsing, validation and scoring live in deterministic, tested code.
- **CLI generated from the SDK.** Every `@operation` becomes `secctl <provider> <op>` with JSON output.
- **Agent on top.** It chooses commands and explains results, and never computes scores itself.
- **Grows by adding a file.** `secctl dev scaffold <name>` creates a provider, and
  the CLI, docs and tool schemas pick it up automatically.

## Agent example

`examples/` has a Claude API agent whose only tool is `secctl`. It follows written
workflows and won't finish until the required commands have run:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run python examples/agent/run_workflow.py cve-triage --input cves="CVE-2024-3400 CVE-2023-4966"
```

## Observability and Azure

- **Telemetry:** OpenTelemetry traces and metrics for every operation and API call. Export
  to Application Insights, any OTLP backend, or a JSONL file (`docs/observability.md`).
- **Foundry agents:** `deploy/azure-functions/` hosts the SDK as a remote MCP server on
  Azure Functions, and `deploy/foundry/create_agent.py` wires it to a Foundry agent
  (`docs/deploy-azure.md`).
- **Agents as a service (proposal):** `deploy/agent-gateway/` puts agents behind Azure API
  Management with per-user, per-team, per-agent and per-deployment token limits from Entra
  groups, plus usage events for chargeback (`docs/agent-gateway.md`).

## Docs

`uv run --group docs mkdocs serve`, then open <http://127.0.0.1:8000>. Start with
*Architecture*, *CLI* and *Using with agents*.

## Development

```bash
uv sync --all-groups
uv run pytest            # offline, HTTP mocked
uv run ruff check . && uv run mypy packages/secsdk/src packages/secctl/src
```

All operations are read-only and OSINT is passive. See `docs/responsible-use.md`.

License: MIT
