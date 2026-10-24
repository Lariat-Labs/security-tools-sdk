# security-tools-sdk

**A structured Python SDK and CLI for security data, built so that an AI agent can drive it, and so that it works without one.**

It ships two packages from one repository:

| Package | What it is | Who uses it |
|---|---|---|
| **`secsdk`** | A provider-based Python SDK: typed inputs and outputs, shared HTTP and retry behaviour, structured errors. | Python code, notebooks, jobs, other tools |
| **`secctl`** | A CLI generated from the SDK. Every SDK operation becomes a command that prints JSON. | People, shell scripts, CI, **agents** |

```bash
$ secctl intel cve-priority CVE-2023-4966 CVE-2014-0160 --no-nvd -f table --fields cve_id,priority,label,epss,in_kev,ransomware
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┓
┃ cve_id        ┃ priority ┃ label   ┃ epss    ┃ in_kev ┃ ransomware ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━┩
│ CVE-2023-4966 │ P1       │ act now │ 0.99999 │ True   │ True       │
│ CVE-2014-0160 │ P1       │ act now │ 0.99999 │ True   │ False      │
└───────────────┴──────────┴─────────┴─────────┴────────┴────────────┘
```

## What's in the box

<!-- secsdk:providers -->

Every provider marked **none** works right after install, with no accounts or API keys.

## The idea in one paragraph

The agent is the **thinnest** layer, not the foundation. All logic that has a
correct answer, such as fetching, parsing, validating or scoring, lives in plain,
deterministic, tested code (the SDK). The CLI exposes that code with a stable
JSON contract. The agent only decides *which* command to run and *how to explain*
the result. When something breaks, you run the same CLI command yourself: if it
works there, the bug is in the prompt layer, not in your data.

[Read the architecture →](architecture.md){ .md-button .md-button--primary }
[Get started →](getting-started.md){ .md-button }
