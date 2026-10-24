# Contributing

```bash
uv sync --all-groups
uv run pytest                      # offline test suite
uv run ruff check . && uv run ruff format --check .
uv run mypy packages/secsdk/src packages/secctl/src
uv run --group docs mkdocs serve   # docs at http://127.0.0.1:8000
```

## Pull request checklist

- [ ] New sources are a provider module (`secctl dev scaffold <name>`), not ad-hoc code in the CLI.
- [ ] Operations are read-only, have a one-line summary docstring, parameter descriptions and at least one example.
- [ ] Upstream JSON is mapped by pure `parse_*` functions with unit tests using realistic fixtures.
- [ ] No network access in tests (respx mocks everything).
- [ ] `uv run mkdocs build --strict` passes. The reference updates itself.

## Repository layout

```text
packages/
  secsdk/                 # the SDK (importable library)
    src/secsdk/
      client.py           # Client: provider access, call(), execute()
      provider.py         # Provider base class
      operation.py        # @operation decorator, Operation metadata
      registry.py         # discovery (built-ins + entry points)
      http.py             # retries, rate limits, cache, error mapping
      providers/          # one module per source  ← grow here
    tests/
  secctl/                 # the CLI (depends on secsdk)
    src/secctl/
      app.py              # built-in commands
      dynamic.py          # operation → click command generation
      output.py           # json / jsonl / table rendering
      scaffold.py         # `secctl dev scaffold`
    tests/
docs/                     # this site (MkDocs Material)
  hooks/reference.py      # generates provider/settings/operation reference
AGENTS.md                 # how agents should use secctl
CLAUDE.md                 # how coding agents should develop this repo
```
