# CLI: `secctl`

`secctl` is generated from the SDK registry. Each provider is a command group and
each operation is a sub-command:

```text
secctl <provider> <operation> [ARGS] [OPTIONS]
```

- **Required** parameters are positional arguments (list parameters accept several values).
- **Optional** parameters are `--options` with dashes (`--max-chars`). List options
  are repeatable *or* comma-separated (`--types MX,TXT --types NS`).
- **Booleans** are `--flag/--no-flag`.
- Input is validated by the SDK, so the CLI and Python enforce the same rules.

## Built-in commands

| Command | Purpose |
|---|---|
| `secctl guide` | Markdown cheat-sheet: conventions plus every operation's usage line |
| `secctl providers` | Providers, operation counts, credential status |
| `secctl ops [provider]` | List operations |
| `secctl describe nvd.cve` | Parameters, JSON schema, output schema, examples |
| `secctl call <op> --params '{…}'` / `-p k=v` | Generic invocation by name |
| `secctl schema [--style anthropic\|openai]` | Tool definitions for LLM function calling |
| `secctl doctor` | Versions, cache, provider configuration |
| `secctl cache clear` | Empty the response cache |
| `secctl dev scaffold <name> [--auth]` | Generate a new provider + test |

## Output contract

By default every command prints **one JSON object** to stdout:

=== "Success"

    ```json
    {
      "ok": true,
      "operation": "kev.lookup",
      "data": { "cve_id": "CVE-2023-4966", "vendor": "Citrix", "ransomware": true },
      "meta": { "sdk_version": "0.1.0", "elapsed_ms": 11.4 }
    }
    ```

=== "Failure"

    ```json
    {
      "ok": false,
      "operation": "tenable.scans",
      "error": {
        "code": "not_configured",
        "type": "ConfigError",
        "message": "Provider 'tenable' is not configured (missing: TENABLE_ACCESS_KEY, TENABLE_SECRET_KEY)",
        "hint": "Set TENABLE_ACCESS_KEY, TENABLE_SECRET_KEY in the environment or a .env file."
      },
      "meta": { "sdk_version": "0.1.0", "elapsed_ms": 0.4 }
    }
    ```

`meta.count` is present when `data` is a list. Fields with null values are omitted.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Internal error (a bug, please report it) |
| 2 | Invalid input, or unknown provider/operation |
| 3 | Provider not configured, or credentials rejected |
| 4 | Upstream API error or rate limit |
| 5 | Not found |

### Formats and trimming

| Option | Effect |
|---|---|
| `-f json` (default) | Pretty JSON envelope. `--compact` puts it on one line |
| `-f jsonl` | One JSON object per line for list results (no envelope) |
| `-f table` | Human-friendly tables via `rich`; errors go to stderr |
| `--fields a,b,c` | Keep only these keys (also applied inside `items`/`rows`) |
| `SECCTL_FORMAT=table` | Change the default format |

Format options work before the sub-command (`secctl -f table kev search`) or after it
(`secctl kev search -f table`).

## Recipes

```bash
# Which CVEs from today's news are actually exploited?
secctl feeds news --days 1 --compact \
  | jq -r '.data.items[].cves[]?' | sort -u | xargs secctl intel cve-priority --no-nvd -f table

# Every company that disclosed a material cyber incident this quarter
secctl sec cyber-incidents --days 90 -f jsonl --fields company,filed,url

# Internet-exposed management ports across all subscriptions
secctl azure check open_management_ports -f table

# KEV vulns present in your Tenable estate, ransomware first
secctl intel kev-exposure --days 30 -f table
```
