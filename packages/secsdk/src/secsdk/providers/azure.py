"""Azure: Resource Graph (KQL) queries and security-focused presets. Read-only.

Credentials, first match wins:

1. ``AZURE_ACCESS_TOKEN``: a bearer token for https://management.azure.com
2. ``AZURE_TENANT_ID`` + ``AZURE_CLIENT_ID`` + ``AZURE_CLIENT_SECRET``: service principal
3. Managed identity (``pip install 'secsdk[azure]'``): automatic inside Azure Functions /
   App Service, or set ``SECSDK_AZURE_MANAGED_IDENTITY=true``
4. The Azure CLI (``az login``), if installed: ``az account get-access-token``

The identity needs *Reader* (and *Security Reader* for Defender data) on the
subscriptions you query.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import SettingsConfigDict

from ..config import ProviderSettings
from ..errors import AuthError, ConfigError
from ..operation import operation
from ..provider import AuthStatus, Provider

ARM = "https://management.azure.com"
LOGIN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_API = "2022-10-01"
SUBS_API = "2022-12-01"


class AzureSettings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="SECSDK_AZURE_")
    access_token: str | None = Field(
        None, validation_alias=AliasChoices("SECSDK_AZURE_ACCESS_TOKEN", "AZURE_ACCESS_TOKEN")
    )
    tenant_id: str | None = Field(
        None, validation_alias=AliasChoices("SECSDK_AZURE_TENANT_ID", "AZURE_TENANT_ID")
    )
    client_id: str | None = Field(
        None, validation_alias=AliasChoices("SECSDK_AZURE_CLIENT_ID", "AZURE_CLIENT_ID")
    )
    client_secret: str | None = Field(
        None, validation_alias=AliasChoices("SECSDK_AZURE_CLIENT_SECRET", "AZURE_CLIENT_SECRET")
    )
    use_cli: bool = Field(True, description="Fall back to `az account get-access-token`")
    managed_identity: bool | None = Field(
        None,
        description="Use the host's managed identity (needs secsdk[azure]). "
        "Default: on automatically inside Azure Functions / App Service.",
    )
    managed_identity_client_id: str | None = Field(
        None, description="Client id of a user-assigned managed identity (omit for system-assigned)"
    )
    subscriptions: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("SECSDK_AZURE_SUBSCRIPTIONS", "AZURE_SUBSCRIPTION_IDS"),
        description="Default subscription ids (JSON list). Empty = all the identity can see.",
    )


class Subscription(BaseModel):
    id: str
    name: str
    state: str | None = None
    tenant_id: str | None = None


class QueryResult(BaseModel):
    total: int
    count: int
    rows: list[dict[str, Any]]
    truncated: bool = False


# Preset queries. Each one is plain KQL, kept here so it is reviewable and testable.
QUERIES: dict[str, str] = {
    "public_ips": """
Resources
| where type =~ 'microsoft.network/publicipaddresses'
| extend ip = tostring(properties.ipAddress), attachedTo = tostring(properties.ipConfiguration.id)
| project name, ip, attachedTo, resourceGroup, subscriptionId, location
| order by name asc""",
    "open_management_ports": """
Resources
| where type =~ 'microsoft.network/networksecuritygroups'
| mv-expand rule = properties.securityRules
| extend r = rule.properties
| where tolower(tostring(r.direction)) == 'inbound' and tolower(tostring(r.access)) == 'allow'
| where tostring(r.sourceAddressPrefix) in ('*', '0.0.0.0/0', 'Internet', 'Any', '::/0')
| extend ports = iff(isnotempty(r.destinationPortRange), pack_array(tostring(r.destinationPortRange)), r.destinationPortRanges)
| mv-expand port = ports to typeof(string)
| where port in ('*', '22', '3389', '445', '1433', '3306', '5432', '5985', '5986', '23', '21')
    or (port contains '-' and toint(split(port, '-')[0]) <= 3389 and toint(split(port, '-')[1]) >= 22)
| project nsg = name, rule = tostring(rule.name), port, protocol = tostring(r.protocol), priority = toint(r.priority), resourceGroup, subscriptionId
| order by priority asc""",
    "storage_weak_config": """
Resources
| where type =~ 'microsoft.storage/storageaccounts'
| extend publicBlob = tobool(properties.allowBlobPublicAccess),
         httpsOnly = tobool(properties.supportsHttpsTrafficOnly),
         minTls = tostring(properties.minimumTlsVersion),
         network = tostring(properties.networkAcls.defaultAction),
         sharedKey = tobool(properties.allowSharedKeyAccess)
| extend issues = array_concat(
    iff(publicBlob == true, pack_array('blob-public-access-allowed'), dynamic([])),
    iff(httpsOnly == false, pack_array('http-allowed'), dynamic([])),
    iff(minTls in ('TLS1_0', 'TLS1_1'), pack_array('weak-min-tls'), dynamic([])),
    iff(network =~ 'Allow', pack_array('open-network-default'), dynamic([])),
    iff(sharedKey != false, pack_array('shared-key-auth-enabled'), dynamic([])))
| where array_length(issues) > 0
| project name, issues, minTls, resourceGroup, subscriptionId, location""",
    "defender_recommendations": """
securityresources
| where type == 'microsoft.security/assessments'
| where tostring(properties.status.code) == 'Unhealthy'
| extend severity = tostring(properties.metadata.severity),
         recommendation = tostring(properties.displayName),
         resourceId = tostring(properties.resourceDetails.Id)
| project recommendation, severity, resourceId, subscriptionId
| order by case(severity == 'High', 0, severity == 'Medium', 1, 2) asc""",
}


class Azure(Provider):
    name = "azure"
    title = "Microsoft Azure"
    description = "Azure Resource Graph (KQL) with security presets: public IPs, open management ports, weak storage, Defender recommendations."
    docs_url = "https://learn.microsoft.com/azure/governance/resource-graph/"
    settings_class = AzureSettings
    settings: AzureSettings
    rate_limit = (10, 5.0)  # Resource Graph allows 15 queries / 5s per user

    def setup(self) -> None:
        self._token: str | None = None
        self._token_exp = 0.0

    # ----- auth --------------------------------------------------------------------

    def _auth_method(self) -> str | None:
        s = self.settings
        if s.access_token:
            return "token"
        if s.tenant_id and s.client_id and s.client_secret:
            return "service_principal"
        if self._use_managed_identity():
            return "managed_identity"
        if s.use_cli and shutil.which("az"):
            return "azure_cli"
        return None

    def _use_managed_identity(self) -> bool:
        wanted = self.settings.managed_identity
        if wanted is None:  # auto: Functions/App Service set IDENTITY_ENDPOINT
            wanted = bool(os.environ.get("IDENTITY_ENDPOINT"))
        return wanted and importlib.util.find_spec("azure.identity") is not None

    def auth_status(self) -> AuthStatus:
        method = self._auth_method()
        return AuthStatus(
            required=True,
            configured=method is not None,
            missing=[]
            if method
            else ["AZURE_ACCESS_TOKEN, a service principal, a managed identity, or `az login`"],
            note=f"auth method: {method}"
            if method
            else "Set a token, a service principal, or install the Azure CLI and run `az login`.",
        )

    def _bearer(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        method = self._auth_method()
        if method == "token":
            self._token, self._token_exp = self.settings.access_token, time.time() + 3000
        elif method == "service_principal":
            data = self.http.post_json(
                LOGIN.format(tenant=self.settings.tenant_id),
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                    "scope": f"{ARM}/.default",
                },
            )
            self._token, self._token_exp = (
                data["access_token"],
                time.time() + int(data.get("expires_in", 3600)),
            )
        elif method == "managed_identity":
            from azure.identity import ManagedIdentityCredential

            cred = ManagedIdentityCredential(client_id=self.settings.managed_identity_client_id)
            try:
                tok = cred.get_token(f"{ARM}/.default")
            except Exception as exc:
                raise AuthError(
                    "Managed identity could not get a token", details=str(exc)[:300]
                ) from exc
            self._token, self._token_exp = tok.token, float(tok.expires_on)
        elif method == "azure_cli":
            self._token, self._token_exp = _cli_token()
        else:
            raise ConfigError("Azure credentials are not configured", hint=self.auth_status().note)
        assert self._token
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer()}"}

    # ----- operations --------------------------------------------------------------

    @operation(auth=True, examples=["secctl azure subscriptions"])
    def subscriptions(self) -> list[Subscription]:
        """Subscriptions visible to the configured identity."""
        data = self.http.get_json(
            f"{ARM}/subscriptions",
            params={"api-version": SUBS_API},
            headers=self._headers(),
            limiter=self.limiter,
        )
        return [
            Subscription(
                id=s["subscriptionId"],
                name=s.get("displayName", ""),
                state=s.get("state"),
                tenant_id=s.get("tenantId"),
            )
            for s in data.get("value", [])
        ]

    @operation(
        auth=True,
        examples=[
            'secctl azure query "Resources | summarize count() by type | top 10 by count_"',
            "secctl azure query \"Resources | where type =~ 'microsoft.compute/virtualmachines'\" --limit 20",
        ],
    )
    def query(
        self,
        kql: Annotated[str, Field(min_length=1, description="Azure Resource Graph KQL query")],
        subscriptions: Annotated[
            list[str], Field(description="Subscription ids (default: configured or all visible)")
        ] = [],
        limit: Annotated[int, Field(ge=1, le=50_000)] = 1000,
    ) -> QueryResult:
        """Run any read-only Resource Graph query (handles paging)."""
        subs = subscriptions or self.settings.subscriptions
        rows: list[dict[str, Any]] = []
        skip: str | None = None
        total = 0
        while True:
            options: dict[str, Any] = {
                "$top": min(1000, limit - len(rows)),
                "resultFormat": "objectArray",
            }
            if skip:
                options["$skipToken"] = skip
            body: dict[str, Any] = {"query": kql.strip(), "options": options}
            if subs:
                body["subscriptions"] = subs
            data = self.http.post_json(
                f"{ARM}/providers/Microsoft.ResourceGraph/resources",
                params={"api-version": GRAPH_API},
                json_body=body,
                headers=self._headers(),
                limiter=self.limiter,
            )
            rows.extend(data.get("data", []))
            total = int(data.get("totalRecords", len(rows)))
            skip = data.get("$skipToken")
            if not skip or len(rows) >= limit:
                break
        return QueryResult(
            total=total, count=len(rows[:limit]), rows=rows[:limit], truncated=total > limit
        )

    @operation(
        auth=True,
        examples=[
            "secctl azure check open_management_ports",
            "secctl azure check storage_weak_config",
        ],
    )
    def check(
        self,
        name: Annotated[
            Literal[
                "public_ips",
                "open_management_ports",
                "storage_weak_config",
                "defender_recommendations",
            ],
            Field(description="Preset security query"),
        ],
        subscriptions: list[str] = [],
        limit: Annotated[int, Field(ge=1, le=50_000)] = 1000,
    ) -> QueryResult:
        """Run a preset security query. Use `secctl azure presets` to see the KQL."""
        return self.query(QUERIES[name], subscriptions=subscriptions, limit=limit)

    @operation
    def presets(self) -> dict[str, str]:
        """The preset KQL queries used by `azure check` (no credentials needed)."""
        return {k: v.strip() for k, v in QUERIES.items()}


def _cli_token() -> tuple[str, float]:
    az = shutil.which("az")
    if not az:
        raise ConfigError("Azure CLI not found")
    try:
        proc = subprocess.run(
            [az, "account", "get-access-token", "--resource", ARM + "/", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AuthError("`az account get-access-token` timed out") from exc
    if proc.returncode != 0:
        raise AuthError(
            "Azure CLI could not get a token",
            hint="Run `az login` first.",
            details=proc.stderr.strip()[:300],
        )
    data = json.loads(proc.stdout)
    exp = float(data.get("expires_on") or time.time() + 1800)
    return data["accessToken"], exp
