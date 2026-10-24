"""Tenable Vulnerability Management (tenable.io) API. Read-only operations.

Credentials: ``TENABLE_ACCESS_KEY`` and ``TENABLE_SECRET_KEY`` (API keys of a
user with at least read access to scans/assets/vulnerabilities).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import SettingsConfigDict

from ..config import ProviderSettings
from ..errors import UpstreamError
from ..operation import operation
from ..provider import AuthStatus, Provider
from ._common import parse_dt, utcnow

Severity = Literal["info", "low", "medium", "high", "critical"]
SEVERITY_IDS = {0: "info", 1: "low", 2: "medium", 3: "high", 4: "critical"}


class TenableSettings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="SECSDK_TENABLE_")
    access_key: str | None = Field(
        None,
        validation_alias=AliasChoices(
            "SECSDK_TENABLE_ACCESS_KEY", "TENABLE_ACCESS_KEY", "TIO_ACCESS_KEY"
        ),
    )
    secret_key: str | None = Field(
        None,
        validation_alias=AliasChoices(
            "SECSDK_TENABLE_SECRET_KEY", "TENABLE_SECRET_KEY", "TIO_SECRET_KEY"
        ),
    )
    url: str = Field(
        "https://cloud.tenable.com",
        validation_alias=AliasChoices("SECSDK_TENABLE_URL", "TENABLE_URL"),
    )
    export_timeout: float = Field(
        600.0, description="Max seconds to wait for a vulnerability export"
    )


class Scan(BaseModel):
    id: int
    uuid: str | None = None
    name: str
    status: str | None = None
    enabled: bool | None = None
    last_modified: datetime | None = None


class Asset(BaseModel):
    id: str
    hostnames: list[str] = []
    ipv4: list[str] = []
    operating_system: list[str] = []
    has_agent: bool | None = None
    last_seen: datetime | None = None


class VulnSummary(BaseModel):
    plugin_id: int
    name: str
    family: str | None = None
    severity: str
    count: int
    vpr: float | None = None
    cvss3: float | None = None


class Finding(BaseModel):
    asset: str = Field(description="Hostname, FQDN or IP of the affected asset")
    asset_uuid: str | None = None
    plugin_id: int
    plugin_name: str
    severity: str
    state: str | None = None
    cves: list[str] = []
    vpr: float | None = None
    cvss3: float | None = None
    port: int | None = None
    first_found: datetime | None = None
    last_found: datetime | None = None
    solution: str | None = None


class Tenable(Provider):
    name = "tenable"
    title = "Tenable Vulnerability Management"
    description = "Scans, assets and vulnerability findings from Tenable Vulnerability Management (tenable.io)."
    docs_url = "https://developer.tenable.com/reference/navigate"
    settings_class = TenableSettings
    settings: TenableSettings
    rate_limit = (20, 1.0)

    def auth_status(self) -> AuthStatus:
        missing = [
            n
            for n, v in (
                ("TENABLE_ACCESS_KEY", self.settings.access_key),
                ("TENABLE_SECRET_KEY", self.settings.secret_key),
            )
            if not v
        ]
        return AuthStatus(required=True, configured=not missing, missing=missing)

    def _headers(self) -> dict[str, str]:
        return {
            "X-ApiKeys": f"accessKey={self.settings.access_key};secretKey={self.settings.secret_key}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.http.get_json(
            self.settings.url + path, params=params, headers=self._headers(), limiter=self.limiter
        )

    @operation(auth=True, examples=["secctl tenable scans"])
    def scans(self) -> list[Scan]:
        """List scans with status and last modification time."""
        data = self._get("/scans")
        return [
            Scan(
                id=s["id"],
                uuid=s.get("uuid"),
                name=s.get("name", ""),
                status=s.get("status"),
                enabled=s.get("enabled"),
                last_modified=parse_dt(s.get("last_modification_date")),
            )
            for s in data.get("scans") or []
        ]

    @operation(auth=True, examples=["secctl tenable assets --limit 50"])
    def assets(self, limit: Annotated[int, Field(ge=1, le=5000)] = 100) -> list[Asset]:
        """List assets (up to 5,000; use an export for larger estates)."""
        data = self._get("/assets")
        return [
            Asset(
                id=a["id"],
                hostnames=(a.get("fqdn") or []) + (a.get("netbios_name") or []),
                ipv4=a.get("ipv4") or [],
                operating_system=a.get("operating_system") or [],
                has_agent=a.get("has_agent"),
                last_seen=parse_dt(a.get("last_seen")),
            )
            for a in (data.get("assets") or [])[:limit]
        ]

    @operation(auth=True, examples=["secctl tenable summary --severity critical"])
    def summary(
        self,
        severity: Annotated[Severity | None, Field(description="Only this severity")] = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 50,
    ) -> list[VulnSummary]:
        """Vulnerability counts grouped by plugin (workbench view), most common first."""
        data = self._get("/workbenches/vulnerabilities")
        rows = []
        for v in data.get("vulnerabilities", []):
            sev = SEVERITY_IDS.get(v.get("severity"), str(v.get("severity")))
            if severity and sev != severity:
                continue
            rows.append(
                VulnSummary(
                    plugin_id=v["plugin_id"],
                    name=v.get("plugin_name", ""),
                    family=v.get("plugin_family"),
                    severity=sev,
                    count=v.get("count", 0),
                    vpr=v.get("vpr_score"),
                    cvss3=v.get("cvss3_base_score"),
                )
            )
        rows.sort(key=lambda r: r.count, reverse=True)
        return rows[:limit]

    @operation(
        auth=True,
        examples=[
            "secctl tenable findings --severity critical --severity high --days 30 --limit 200"
        ],
    )
    def findings(
        self,
        severity: Annotated[list[Severity], Field(description="Severities to include")] = [
            "critical",
            "high",
        ],
        days: Annotated[int | None, Field(ge=1, description="Seen in the last N days")] = 30,
        state: Annotated[
            list[Literal["open", "reopened", "fixed"]], Field(description="Finding states")
        ] = ["open", "reopened"],
        limit: Annotated[int, Field(ge=1, le=100_000)] = 500,
    ) -> list[Finding]:
        """Per-asset vulnerability findings via the export API (waits for the export to finish)."""
        filters: dict[str, Any] = {"severity": severity, "state": [s.upper() for s in state]}
        if days:
            filters["since"] = int((utcnow() - timedelta(days=days)).timestamp())
        start = self.http.post_json(
            self.settings.url + "/vulns/export",
            json_body={"num_assets": 500, "filters": filters},
            headers=self._headers(),
            limiter=self.limiter,
        )
        uuid = start["export_uuid"]
        deadline = time.monotonic() + self.settings.export_timeout
        done: set[int] = set()
        out: list[Finding] = []
        while True:
            status = self._get(f"/vulns/export/{uuid}/status")
            for chunk in status.get("chunks_available", []):
                if chunk in done:
                    continue
                for raw in self._get(f"/vulns/export/{uuid}/chunks/{chunk}"):
                    out.append(parse_finding(raw))
                done.add(chunk)
                if len(out) >= limit:
                    self.http.request(
                        "POST",
                        f"{self.settings.url}/vulns/export/{uuid}/cancel",
                        headers=self._headers(),
                    )
                    return out[:limit]
            st = status.get("status")
            if st == "FINISHED":
                return out[:limit]
            if st in ("ERROR", "CANCELLED"):
                raise UpstreamError(f"Tenable export {uuid} ended with status {st}")
            if time.monotonic() > deadline:
                raise UpstreamError(
                    f"Tenable export {uuid} did not finish within {self.settings.export_timeout:.0f}s"
                )
            time.sleep(3)


def parse_finding(raw: dict[str, Any]) -> Finding:
    asset = raw.get("asset", {})
    plugin = raw.get("plugin", {})
    port = raw.get("port", {}) or {}
    return Finding(
        asset=asset.get("hostname")
        or asset.get("fqdn")
        or asset.get("ipv4")
        or asset.get("uuid", "unknown"),
        asset_uuid=asset.get("uuid"),
        plugin_id=plugin.get("id", 0),
        plugin_name=plugin.get("name", ""),
        severity=str(raw.get("severity", "")).lower(),
        state=raw.get("state"),
        cves=plugin.get("cve") or [],
        vpr=(plugin.get("vpr") or {}).get("score"),
        cvss3=plugin.get("cvss3_base_score"),
        port=port.get("port") or None,
        first_found=parse_dt(raw.get("first_found")),
        last_found=parse_dt(raw.get("last_found")),
        solution=plugin.get("solution"),
    )
