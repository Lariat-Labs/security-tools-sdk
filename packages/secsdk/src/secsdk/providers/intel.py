"""Cross-source analysis. Deterministic rules that combine other providers.

This is where "steps with a correct answer" live: the agent asks *which* CVEs
matter; these functions decide *how* they are scored, the same way every time.
The scoring rules are plain functions so they can be unit-tested and audited.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ..errors import NotFoundError, SecSDKError
from ..operation import operation
from ..provider import AuthStatus, Provider
from ._common import CveId
from .epss import EPSS, EPSSScore
from .kev import KEV, KEVEntry
from .nvd import CVE, NVD
from .tenable import Severity, Tenable

Priority = Literal["P1", "P2", "P3", "P4"]
LABELS = {"P1": "act now", "P2": "high", "P3": "moderate", "P4": "low"}


class CvePriority(BaseModel):
    cve_id: str
    priority: Priority
    label: str
    reasons: list[str]
    cvss: float | None = None
    epss: float | None = None
    epss_percentile: float | None = None
    in_kev: bool = False
    ransomware: bool = False
    kev_due: date | None = None
    summary: str | None = None


class ExposedCve(BaseModel):
    cve_id: str
    priority: Priority
    assets: list[str]
    asset_count: int
    plugins: list[str]
    kev_added: date
    kev_due: date | None = None
    ransomware: bool = False
    epss: float | None = None


def score_cve(
    cve_id: str,
    *,
    cvss: float | None = None,
    epss: float | None = None,
    kev: KEVEntry | None = None,
) -> tuple[Priority, list[str]]:
    """The prioritisation rules. Order matters: first match wins.

    * **P1**: listed in CISA KEV (exploited in the wild).
    * **P2**: EPSS >= 0.5, or CVSS >= 9.0 with EPSS >= 0.1.
    * **P3**: CVSS >= 7.0, or EPSS >= 0.1.
    * **P4**: everything else.
    """
    reasons: list[str] = []
    if kev:
        reasons.append(
            f"In CISA KEV since {kev.date_added}"
            + (f", federal due date {kev.due_date}" if kev.due_date else "")
        )
        if kev.ransomware:
            reasons.append("Known ransomware campaign use")
    if epss is not None:
        reasons.append(f"EPSS {epss:.3f}")
    if cvss is not None:
        reasons.append(f"CVSS {cvss:.1f}")

    if kev:
        return "P1", reasons
    e = epss or 0.0
    c = cvss or 0.0
    if e >= 0.5 or (c >= 9.0 and e >= 0.1):
        return "P2", reasons
    if c >= 7.0 or e >= 0.1:
        return "P3", reasons
    if not reasons:
        reasons.append("No KEV listing, EPSS or CVSS data found")
    return "P4", reasons


class Intel(Provider):
    name = "intel"
    title = "Intel (cross-source)"
    description = "Deterministic analysis combining other providers: CVE prioritisation and KEV exposure in Tenable."

    @operation(
        examples=[
            "secctl intel cve-priority CVE-2021-44228 CVE-2023-4966 CVE-2019-0708",
            "secctl intel cve-priority CVE-2024-3400 --no-nvd",
        ]
    )
    def cve_priority(
        self,
        cve_ids: Annotated[list[CveId], Field(min_length=1, max_length=200)],
        nvd: Annotated[
            bool, Field(description="Fetch CVSS from NVD (slow without an API key)")
        ] = True,
    ) -> list[CvePriority]:
        """Prioritise CVEs with KEV + EPSS (+ NVD CVSS). Sorted most urgent first."""
        ids = list(dict.fromkeys(cve_ids))
        kev_index = self.client.get(KEV).index()
        epss: dict[str, EPSSScore] = {s.cve_id: s for s in self.client.get(EPSS).score(ids)}
        out = []
        for cid in ids:
            record: CVE | None = None
            if nvd:
                try:
                    record = self.client.get(NVD).cve(cid)
                except NotFoundError:
                    record = None
            kev = kev_index.get(cid)
            e = epss.get(cid)
            cvss = record.cvss.score if record and record.cvss else None
            prio, reasons = score_cve(cid, cvss=cvss, epss=e.epss if e else None, kev=kev)
            out.append(
                CvePriority(
                    cve_id=cid,
                    priority=prio,
                    label=LABELS[prio],
                    reasons=reasons,
                    cvss=cvss,
                    epss=e.epss if e else None,
                    epss_percentile=e.percentile if e else None,
                    in_kev=kev is not None,
                    ransomware=bool(kev and kev.ransomware),
                    kev_due=kev.due_date if kev else None,
                    summary=(
                        record.description[:300]
                        if record
                        else (kev.description[:300] if kev else None)
                    ),
                )
            )
        out.sort(key=lambda p: (p.priority, -(p.epss or 0), -(p.cvss or 0)))
        return out

    @operation(auth=True, examples=["secctl intel kev-exposure --days 30"])
    def kev_exposure(
        self,
        severity: Annotated[list[Severity], Field(description="Tenable severities to scan")] = [
            "critical",
            "high",
            "medium",
        ],
        days: Annotated[int | None, Field(ge=1)] = 30,
        limit: Annotated[int, Field(ge=1, le=100_000)] = 5000,
    ) -> list[ExposedCve]:
        """Which Tenable findings are CISA KEV vulnerabilities? Grouped by CVE, ransomware first."""
        tenable = self.client.get(Tenable)
        findings = tenable.findings(severity=severity, days=days, limit=limit)
        kev_index = self.client.get(KEV).index()
        grouped: dict[str, dict] = {}
        for f in findings:
            for cid in f.cves:
                if cid in kev_index:
                    g = grouped.setdefault(cid, {"assets": set(), "plugins": set()})
                    g["assets"].add(f.asset)
                    g["plugins"].add(f.plugin_name)
        try:
            epss = (
                {s.cve_id: s.epss for s in self.client.get(EPSS).score(list(grouped))}
                if grouped
                else {}
            )
        except SecSDKError:
            epss = {}
        out = []
        for cid, g in grouped.items():
            kev = kev_index[cid]
            out.append(
                ExposedCve(
                    cve_id=cid,
                    priority="P1",
                    assets=sorted(g["assets"])[:50],
                    asset_count=len(g["assets"]),
                    plugins=sorted(g["plugins"])[:10],
                    kev_added=kev.date_added,
                    kev_due=kev.due_date,
                    ransomware=kev.ransomware,
                    epss=epss.get(cid),
                )
            )
        out.sort(key=lambda x: (not x.ransomware, -x.asset_count))
        return out

    def auth_status(self) -> AuthStatus:
        # cve_priority is keyless; kev_exposure inherits Tenable's credentials,
        # and the Tenable call raises a precise ConfigError if they are missing.
        tenable = self.client.get(Tenable).auth_status()
        note = "cve_priority needs no keys. kev_exposure needs Tenable: " + (
            "configured." if tenable.configured else f"missing {', '.join(tenable.missing)}."
        )
        return AuthStatus(required=False, configured=True, note=note)
