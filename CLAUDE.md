# CLAUDE.md: developing this repository

uv workspace with two packages: `packages/secsdk` (SDK) and `packages/secctl` (CLI,
generated from the SDK registry). Docs are MkDocs Material in `docs/`.

## Commands

```bash
uv sync --all-groups
uv run pytest -q                                   # must pass, offline
uv run ruff check . && uv run ruff format .
uv run mypy packages/secsdk/src packages/secctl/src
uv run --group docs mkdocs build --strict
uv run secctl <provider> <operation> ...           # verify behaviour for real
```

## How to add or change functionality

- New data source → `uv run secctl dev scaffold <name> [--auth]`, then implement it in
  `packages/secsdk/src/secsdk/providers/<name>.py`. Don't add provider-specific code to the CLI.
- Logic with a correct answer (parsing, scoring, filtering) goes in the SDK as pure
  functions with unit tests. The CLI only formats output.
- Operations are read-only. Map upstream JSON with `parse_*` functions and test them
  with realistic fixtures. Tests never hit the network (use respx).
- First docstring line = CLI help and tool description. Give every parameter a `Field(description=...)`.
- Verify with both `uv run pytest` and a real `uv run secctl ...` call (keyless
  providers work without credentials) before calling a change done.
- Never commit secrets; `.env` is git-ignored.
