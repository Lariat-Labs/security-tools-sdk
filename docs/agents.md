# Using with agents

The agent in this design has **one tool: a shell that can run `secctl`**. It never
imports the SDK, and it never computes scores or parses APIs itself.

## Why the CLI is the agent's interface

- **One contract.** The agent sees the same JSON envelope and exit codes as a human or a CI job.
- **Self-describing.** `secctl guide`, `secctl ops` and `secctl describe <op>` tell the
  agent what exists and what parameters each operation takes. Nothing has to be pasted into the prompt.
- **Reproducible.** Every agent action is a command you can copy and run yourself.
  If it works for you and not for the agent, the bug is in the prompt layer.
- **Safe by construction.** Every operation is read-only. The agent can explore freely.
- **Grows automatically.** A new provider becomes a new command, and the agent
  discovers it from `secctl guide` on its next run.

## Recommended agent loop

1. Run `secctl guide` once at the start to learn the commands and conventions.
2. Run `secctl providers` to learn what is configured. Don't plan around tools that will fail.
3. For an unfamiliar operation, run `secctl describe <provider>.<operation>`.
4. Run commands. Read `ok`. On failure, read `error.code` and `error.hint`:
    - `invalid_input`: fix the arguments (details name the parameter).
    - `not_configured`: tell the user which variable is missing. Don't retry.
    - `upstream_error` / `rate_limited`: retry once later, or report it.
    - `not_found`: a valid answer (e.g. "not in KEV"), not a failure.
5. Use `--fields` to keep outputs small, and `--compact` to save tokens.
6. Explain results and make judgement calls. Leave the computing to `secctl`.

## System prompt snippet

```text
You are a security analyst assistant. You have a shell with the `secctl` CLI.
- Start by running `secctl guide` and `secctl providers`.
- Only use secctl for security data; do not guess facts that secctl can look up.
- Output is JSON: check "ok". On errors, follow "error.hint".
- Use --fields and --compact to keep output small.
- Scores and priorities come from secctl (e.g. `secctl intel cve-priority`); do
  not invent your own scoring. Explain *why* using the "reasons" field.
- All secctl operations are read-only and passive. Do not run other network tools.
```

## Claude Code

The repository includes an [`AGENTS.md`](https://github.com/Lariat-Labs/security-tools-sdk/blob/main/AGENTS.md)
(usage rules for agents) and a `CLAUDE.md` (development rules). That gives you two loops:

**Analyst loop**: ask questions and the agent answers with `secctl`:

> Which of the CVEs mentioned in security news this week are in KEV, and which
> SEC registrants disclosed cyber incidents in the same period?

**Builder loop**: the agent extends the SDK and verifies with the CLI:

> Add a GreyNoise community provider with an `ip` operation. Scaffold it with
> `secctl dev scaffold greynoise`, implement it, add tests, and verify with
> `uv run pytest` and `uv run secctl greynoise ip 8.8.8.8` until both pass.

The builder loop works because every feature can be run and checked from the
terminal, so the agent can write, run, read and fix without you in the middle.

## Function-calling instead of a shell

If your runtime prefers native tool calls over a shell, generate tool definitions
from the same registry and map each call to `Client.execute`:

```bash
secctl schema --style anthropic > tools.json   # or --style openai
```

```python
from secsdk import Client
from secsdk.tools import tool_definitions

client = Client()
tools = tool_definitions(style="anthropic")
by_tool = {op.tool_name: op.fqname for op in client.operations()}


def handle_tool_call(name: str, arguments: dict) -> dict:
    return client.execute(by_tool[name], arguments).to_dict()
```

The agent still only orchestrates. `execute` returns the same envelope the CLI prints.

!!! tip "Keep the tool list focused"
    Thirty-plus tools is a lot for one prompt. Use `--provider` (CLI) or `providers=[...]`
    (Python) to give each agent only the providers its job needs.
