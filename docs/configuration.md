# Configuration

All configuration is read from **environment variables**, or from a `.env` file in
the current directory. `secctl --env-file path/to/file` loads another file;
variables already set in the environment take precedence.

```bash
cp .env.example .env    # then fill in what you have
secctl providers -f table
secctl doctor
```

Where a vendor has a well-known variable name (`AZURE_TENANT_ID`, `NVD_API_KEY`, …),
it is accepted alongside the `SECSDK_`-prefixed name.

!!! warning "Keep secrets out of git"
    `.env` is in `.gitignore`. Never commit real keys. For CI, use your platform's
    secret store and inject them as environment variables.

## Variables

The tables below are generated from the settings classes in the SDK.

<!-- secsdk:settings -->

## Credentials by provider

| Provider | What you need | Where to get it |
|---|---|---|
| `nvd` | Nothing. Optional `NVD_API_KEY` raises the rate limit 10× | [nvd.nist.gov/developers/request-an-api-key](https://nvd.nist.gov/developers/request-an-api-key) |
| `sec` | No key. A User-Agent with contact email is **required** by the SEC | [SEC fair access](https://www.sec.gov/os/accessing-edgar-data) |
| `feedly` | `FEEDLY_ACCESS_TOKEN` (`feedly.search` works without it) | Feedly account → developer access token |
| `tenable` | `TENABLE_ACCESS_KEY` + `TENABLE_SECRET_KEY` | Tenable VM → Settings → My Account → API Keys |
| `azure` | One of: `AZURE_ACCESS_TOKEN` · a service principal (`AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET`) · `az login` | Give the identity **Reader** (+ **Security Reader** for Defender) |

### Least privilege

Every operation in this repository is **read-only**. Create dedicated, read-only
credentials for it:

- **Tenable**: a user with a *Basic* or *Scan Operator* role, or a custom role
  with read access to scans, assets and vulnerabilities.
- **Azure**: `Reader` at the subscription or management-group scope, plus
  `Security Reader` if you use `azure check defender_recommendations`.

## Cache and rate limits

| Setting | Default | Notes |
|---|---|---|
| `SECSDK_CACHE_ENABLED` | `true` | Only public sources use the cache. `secctl --no-cache` bypasses it for one run |
| `SECSDK_CACHE_DIR` | `~/.cache/secsdk` | `secctl cache clear` empties it |
| `SECSDK_RATE_LIMIT` | `true` | Leave on unless you are testing against mocks |
