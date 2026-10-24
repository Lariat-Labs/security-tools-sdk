# Examples: a Claude agent that works through `secctl`

A small agent that follows a written **workflow** and gets every fact from the
`secctl` CLI. It has exactly one tool, `secctl`, so it cannot browse, run shell
commands or import the SDK. Every command it runs is logged, so you can re-run it yourself.

```text
examples/
  agent/
    secctl_agent.py    # the tool, its safety checks, and the agent loop
    run_workflow.py    # command-line runner
  workflows/           # directions the agent follows (Markdown + YAML front matter)
    cve-triage.md
    domain-posture.md
    threat-brief.md
  tests/test_agent.py  # offline tests (scripted fake model, real secctl)
  output/              # reports + command logs (git-ignored)
```

## Run it

```bash
uv sync                                   # installs the `examples` group (anthropic, pyyaml)
export ANTHROPIC_API_KEY=sk-ant-...       # or: ant auth login

uv run python examples/agent/run_workflow.py --list

uv run python examples/agent/run_workflow.py cve-triage \
  --input cves="CVE-2024-3400 CVE-2023-4966 CVE-2021-44228"

uv run python examples/agent/run_workflow.py domain-posture --input domain=example.com
uv run python examples/agent/run_workflow.py threat-brief --input days=2
```

While it runs, each command the agent issues is printed to stderr with its reason:

```text
▶ cve-triage with claude-opus-5-5 (effort=high)
  $ secctl guide   (exit 0, 0.4s)
    ↳ Step 1: learn the available commands
  $ secctl intel cve-priority CVE-2024-3400 CVE-2023-4966 CVE-2021-44228 --compact   (exit 0, 3.1s)
    ↳ Step 2: authoritative priority
  ...
```

The final Markdown report is printed to stdout and saved to `examples/output/` next to a
`.commands.json` log (every command, its reason, exit code, timing and token usage).

Options: `--model` (default `claude-opus-5-5`), `--effort low|medium|high|xhigh|max`
(default `high`), `--out DIR`, `--quiet`.

## How the CLI requirement is enforced

1. **One tool.** The model's only tool is `secctl`. The system prompt says a fact
   that didn't come from secctl output can't go in the report.
2. **Required steps.** Each workflow lists `required_commands` (e.g. `intel cve-priority`).
   If the model tries to finish before running them, the loop doesn't accept the answer.
   It tells the model which commands are missing (up to two times), and if they still
   aren't run, the report is marked `incomplete`.
3. **Safe execution.** Arguments are passed to `secctl` as a list with no shell, so
   `; rm -rf /` is just a bad domain name. `secctl dev` (writes files), `secctl cache`
   and `--env-file` are blocked. Output over 20,000 characters is cut, and the model is
   told how to narrow its query. Each command has a 5-minute timeout.
4. **Read-only.** Every secctl operation is read-only and passive, so the agent can explore freely.

The agent sees the same credentials as your shell. `secctl providers` tells it which
sources are configured, and the workflows say to skip unconfigured steps and report
that they were skipped.

## Write your own workflow

Add `examples/workflows/<name>.md`:

```markdown
---
name: vendor-watch
description: What's exploited for one vendor lately?
inputs:
  vendor:
    description: Vendor name as KEV spells it, e.g. Ivanti
  days:
    description: Look-back window
    default: "30"
max_turns: 20
required_commands:     # command prefixes that must run before the report is accepted
  - kev search
  - intel cve-priority
---
Report on **{vendor}** for the last **{days}** days.

1. Run `secctl kev search --vendor "{vendor}" --days {days}`.
2. Run `secctl intel cve-priority --no-nvd` on those CVEs.
3. Write a report: table of CVEs with priority and KEV due date, then the 3 actions to take first.
```

`{placeholders}` are filled from `--input key=value` or the declared defaults. Then run it with
`run_workflow.py vendor-watch --input vendor=Ivanti`. `examples/tests/test_agent.py` automatically
checks that every workflow loads and renders.

## Model and API notes

- Uses the Anthropic Python SDK with a hand-written tool-use loop. The SDK's tool runner
  can't refuse a final answer until required commands have run, so the loop is written out.
- Default model `claude-opus-5-5`. Thinking is adaptive and always on; `--effort` controls depth.
- `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) is enabled. If a safety
  classifier declines part of a request (security topics can trigger the cyber classifier),
  the API continues on a fallback model in the same call, and the runner prints `[fallback]`.
  Remove those two lines in `SecctlAgent._create` to turn it off.
- The system prompt (including `AGENTS.md`) is stable and marked for prompt caching, so later
  turns read it from cache. The usage line at the end shows how many tokens were cached.
- Conversation history is only ever appended to, never edited, as preserved thinking requires.

## Tests

```bash
uv run pytest examples/tests -q   # no API key or network needed
```
