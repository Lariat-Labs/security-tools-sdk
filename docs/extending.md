# Extending the SDK

The SDK is designed to grow one provider at a time. **Adding a module is the only
step needed:** the CLI commands, docs reference, `secctl guide` and tool schemas all
update automatically.

## 1. Scaffold

```bash
uv run secctl dev scaffold greynoise          # keyless source
uv run secctl dev scaffold shodan --auth      # needs an API key
```

This writes `packages/secsdk/src/secsdk/providers/<name>.py` and a matching test in
`packages/secsdk/tests/providers/`. Both work immediately against a placeholder API.

```bash
uv run secctl greynoise --help
uv run pytest packages/secsdk/tests/providers/test_greynoise.py
```

## 2. Implement

A provider is a class with metadata and `@operation` methods:

```python
from typing import Annotated
from pydantic import BaseModel, Field

from ..operation import operation
from ..provider import Provider
from ._common import IPAddress


class IPContext(BaseModel):
    ip: str
    noise: bool
    riot: bool
    classification: str | None = None


class GreyNoise(Provider):
    name = "greynoise"
    title = "GreyNoise Community"
    description = "Is this IP mass-scanning the internet, or a known benign service?"
    docs_url = "https://docs.greynoise.io/reference/get_v3-community-ip"
    rate_limit = (10, 60.0)

    @operation(examples=["secctl greynoise ip 8.8.8.8"])
    def ip(self, ip: IPAddress) -> IPContext:
        """Community classification for one IP."""
        data = self.http.get_json(
            f"https://api.greynoise.io/v3/community/{ip}", limiter=self.limiter
        )
        return IPContext(
            ip=ip,
            noise=data.get("noise", False),
            riot=data.get("riot", False),
            classification=data.get("classification"),
        )
```

### Rules that keep the SDK consistent

| Do | Because |
|---|---|
| Put the **first docstring line** in plain words | It becomes CLI help, the guide line and the tool description |
| Use `Annotated[..., Field(description=...)]` on parameters | Descriptions flow into `--help`, docs and JSON schema |
| Reuse types from `_common` (`CveId`, `Domain`, `IPAddress`) | Same validation and normalisation everywhere |
| Return **pydantic models**, kept small | Typed for Python, clean JSON for the CLI |
| Map upstream JSON with **pure `parse_*` functions** | Unit-testable without HTTP |
| Call `self.http` with `limiter=self.limiter` | Retries, rate limits and error mapping come for free |
| Pass `cache_ttl=` only for **public** data | Never cache authenticated responses |
| Raise `NotFoundError` / `InvalidInputError` deliberately | Agents branch on `error.code` |
| Keep operations **read-only** | The agent can explore without risk |
| Put cross-source logic in `intel`, as pure functions | "Correct answer" logic stays testable and auditable |

### Credentials

Give the provider a settings class and an `auth_status`, then mark operations `auth=True`:

```python
class ShodanSettings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="SECSDK_SHODAN_")
    api_key: str | None = Field(
        None, validation_alias=AliasChoices("SECSDK_SHODAN_API_KEY", "SHODAN_API_KEY")
    )


class Shodan(Provider):
    settings_class = ShodanSettings

    def auth_status(self) -> AuthStatus:
        ok = bool(self.settings.api_key)
        return AuthStatus(required=True, configured=ok, missing=[] if ok else ["SHODAN_API_KEY"])
```

Unconfigured calls then fail with exit code 3 and a hint, before any network request.
The [configuration page](configuration.md) picks up the new variables automatically.

### Using other providers

Composite operations call other providers through the client. Use `client.get(...)` for typed access:

```python
from .kev import KEV

kev_index = self.client.get(KEV).index()
```

## 3. Test

Tests run offline with [respx](https://lundberg.github.io/respx/) mocking HTTP. The
`client` fixture disables the cache and rate limits, and scrubs credentials from the environment.

```python
@respx.mock
def test_ip(client):
    respx.get("https://api.greynoise.io/v3/community/8.8.8.8").mock(
        return_value=httpx.Response(200, json={"noise": False, "riot": True})
    )
    assert client.greynoise.ip("8.8.8.8").riot
```

`packages/secctl/tests/test_cli.py::test_every_operation_has_a_working_command`
automatically checks that your new operations produce working CLI commands.

## External plugins

Providers can live in a separate package. Register them with an entry point and
`secsdk` loads them at start-up:

```toml
# your-package/pyproject.toml
[project.entry-points."secsdk.providers"]
internal_cmdb = "acme_secsdk.cmdb:CMDB"
```

This is the way to add private or company-specific sources (an internal CMDB, a
ticketing system) without forking the public repository.
