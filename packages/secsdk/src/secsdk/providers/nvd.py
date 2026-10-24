"""NIST National Vulnerability Database (CVE API 2.0). Works without a key; a key raises limits."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import SettingsConfigDict

from ..config import ProviderSettings
from ..errors import NotFoundError
from ..operation import operation
from ..provider import AuthStatus, Provider
from ._common import CveId, parse_dt, utcnow

API = "https://services.nvd.nist.gov/rest/json/cves/2.0"


class NVDSettings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="SECSDK_NVD_")
    api_key: str | None = Field(
        None, validation_alias=AliasChoices("SECSDK_NVD_API_KEY", "NVD_API_KEY")
    )


class CVSS(BaseModel):
    version: str
    score: float
    severity: str | None = None
    vector: str | None = None
    source: str | None = None


class CVE(BaseModel):
    id: str
    published: datetime | None = None
    last_modified: datetime | None = None
    status: str | None = None
    description: str = ""
    cvss: CVSS | None = Field(None, description="Highest-version primary CVSS score available")
    cwes: list[str] = []
    in_kev: bool = Field(False, description="NVD's own CISA KEV flag")
    references: list[str] = []


class NVD(Provider):
    name = "nvd"
    title = "NIST NVD"
    description = "CVE records with CVSS scores, CWEs and references from the National Vulnerability Database."
    docs_url = "https://nvd.nist.gov/developers/vulnerabilities"
    settings_class = NVDSettings
    settings: NVDSettings

    def limits(self) -> tuple[int, float]:
        # Published limits: 5 requests / 30s without a key, 50 / 30s with one.
        return (50, 30.0) if self.settings.api_key else (5, 30.0)

    def auth_status(self) -> AuthStatus:
        return AuthStatus(
            required=False,
            configured=True,
            note="Optional NVD_API_KEY raises the rate limit from 5 to 50 requests per 30s."
            + ("" if not self.settings.api_key else " (key set)"),
        )

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"apiKey": self.settings.api_key} if self.settings.api_key else None
        return self.http.get_json(
            API, params=params, headers=headers, limiter=self.limiter, timeout=60
        )

    @operation(examples=["secctl nvd cve CVE-2021-44228"])
    def cve(self, cve_id: CveId) -> CVE:
        """Fetch a single CVE record."""
        data = self._get({"cveId": cve_id})
        vulns = data.get("vulnerabilities") or []
        if not vulns:
            raise NotFoundError(f"{cve_id} not found in NVD")
        return parse_cve(vulns[0]["cve"])

    @operation(examples=['secctl nvd search --keyword "fortinet" --days 30 --severity CRITICAL'])
    def search(
        self,
        keyword: Annotated[str | None, Field(description="Free-text keyword search")] = None,
        days: Annotated[
            int, Field(ge=1, le=120, description="Published in the last N days (NVD max 120)")
        ] = 7,
        severity: Annotated[
            Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] | None,
            Field(description="CVSS v3 severity filter"),
        ] = None,
        kev_only: Annotated[bool, Field(description="Only CVEs in CISA KEV")] = False,
        limit: Annotated[int, Field(ge=1, le=2000)] = 50,
    ) -> list[CVE]:
        """Search recently published CVEs by keyword, severity and KEV status."""
        end = utcnow()
        start = end - timedelta(days=days)
        params: dict[str, Any] = {
            "pubStartDate": start.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "pubEndDate": end.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "resultsPerPage": limit,
            "keywordSearch": keyword,
            "cvssV3Severity": severity,
        }
        if kev_only:
            params["hasKev"] = ""
        data = self._get(params)
        return [parse_cve(v["cve"]) for v in data.get("vulnerabilities", [])][:limit]


def parse_cve(raw: dict[str, Any]) -> CVE:
    desc = next((d["value"] for d in raw.get("descriptions", []) if d.get("lang") == "en"), "")
    cwes = sorted(
        {
            d["value"]
            for w in raw.get("weaknesses", [])
            for d in w.get("description", [])
            if d.get("value", "").startswith("CWE-")
        }
    )
    return CVE(
        id=raw["id"],
        published=parse_dt(raw.get("published")),
        last_modified=parse_dt(raw.get("lastModified")),
        status=raw.get("vulnStatus"),
        description=desc,
        cvss=_best_cvss(raw.get("metrics", {})),
        cwes=cwes,
        in_kev=bool(raw.get("cisaExploitAdd")),
        references=[r["url"] for r in raw.get("references", [])][:25],
    )


def _best_cvss(metrics: dict[str, Any]) -> CVSS | None:
    for key, version in (
        ("cvssMetricV40", "4.0"),
        ("cvssMetricV31", "3.1"),
        ("cvssMetricV30", "3.0"),
        ("cvssMetricV2", "2.0"),
    ):
        entries = metrics.get(key) or []
        if not entries:
            continue
        entry = next((e for e in entries if e.get("type") == "Primary"), entries[0])
        data = entry.get("cvssData", {})
        return CVSS(
            version=version,
            score=float(data.get("baseScore", 0)),
            severity=data.get("baseSeverity") or entry.get("baseSeverity"),
            vector=data.get("vectorString"),
            source=entry.get("source"),
        )
    return None
