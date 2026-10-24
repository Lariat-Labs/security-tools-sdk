# 01 · The SDK and CLI the agents use

*The tools themselves: where the security facts come from.*

`secsdk` is a Python library with *providers* (KEV, EPSS, NVD, OSINT, feeds, Tenable,
Azure…), each exposing read-only *operations*. `secctl` is a CLI generated from the same
registry. Both return the same JSON envelope, `{ok, operation, data | error, meta}`, which is
what makes them safe to hand to a model: it always knows whether a call worked and why not.

## Steps

!!! example "Run: read the menu an agent reads"
    ```bash
    uv run secctl guide | head -40
    ```

!!! example "Run: call one operation and read the envelope"
    ```bash
    uv run secctl kev lookup CVE-2023-4966
    ```
    Look for `"ok": true`, the vendor (Citrix), and `ransomware: true`.

!!! example "Run: the composite operation the triage agent relies on"
    ```bash
    uv run secctl intel cve-priority CVE-2024-3400 CVE-2023-4966 --no-nvd -f table
    ```
    Both come back **P1**, with reasons. The agent is told never to re-score: priorities come
    from this function, not from the model.

??? question "Check: what turns a secsdk operation into something the model can call?"
    `secsdk.tools.tool_definition()` turns each operation's typed signature into a JSON Schema
    "function". The runtime offers only the operations of the providers listed in the agent's
    YAML, so `cve-triage` can't call Azure or Tenable.

[Next: identity and tokens →](02-identity.md)
