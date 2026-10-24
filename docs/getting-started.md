# Getting started

## Install

The repository is a [uv](https://docs.astral.sh/uv/) workspace with two packages.

```bash
git clone https://github.com/Lariat-Labs/security-tools-sdk
cd security-tools-sdk
uv sync                 # creates .venv with secsdk + secctl (editable)
uv run secctl --help
```

Python 3.11+ is required. Without uv, `pip install -e packages/secsdk -e packages/secctl`
works too.

## First commands (no credentials needed)

```bash
uv run secctl guide                                   # cheat-sheet of every command
uv run secctl providers -f table                      # what's available / configured

uv run secctl kev lookup CVE-2023-4966                # is it exploited in the wild?
uv run secctl epss score CVE-2021-44228 CVE-2019-0708 # exploitation probability
uv run secctl intel cve-priority CVE-2024-3400 CVE-2023-4966 -f table

uv run secctl osint profile example.com               # DNS, RDAP, SPF/DMARC
uv run secctl osint subdomains example.com            # certificate transparency
uv run secctl osint exposure 1.1.1.1                  # Shodan InternetDB

uv run secctl feeds news --days 2 -f table --fields source,title,published
```

The SEC provider needs no key, but the SEC requires a contact in the User-Agent:

```bash
export SECSDK_SEC_USER_AGENT="Your Org you@your-org.example"
uv run secctl sec cyber-incidents --days 60 -f table --fields company,filed,form,url
uv run secctl sec filings MSFT --forms 10-K --limit 1
uv run secctl sec document "<url from above>" --section 1C
```

## Use the SDK from Python

```python
from secsdk import Client
from secsdk.providers.kev import KEV

with Client() as sec:
    # attribute access: quick and dynamic
    entry = sec.kev.lookup("CVE-2023-4966")
    print(entry.vendor, entry.ransomware)

    # typed access: full IDE completion and mypy checking
    recent = sec.get(KEV).search(vendor="microsoft", days=30)

    # by name, the same path the CLI and agents use
    scores = sec.call("epss.score", cve_ids=["CVE-2021-44228"])

    # never raises: returns the JSON envelope the CLI prints
    result = sec.execute("osint.dns", {"name": "example.com", "types": ["MX"]})
    print(result.ok, result.to_dict())
```

All return values are pydantic models, so `.model_dump()` and `.model_dump_json()` are available.

## Add credentials when you want them

Copy `.env.example` to `.env` and fill in what you have. `secctl providers` shows
what is configured. See [Configuration](configuration.md).

## Run the tests

```bash
uv run pytest           # offline: all HTTP is mocked
uv run ruff check . && uv run mypy packages/*/src
uv run --group docs mkdocs serve   # this documentation, live-reloading
```
