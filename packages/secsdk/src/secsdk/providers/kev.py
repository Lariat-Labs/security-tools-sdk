"""CISA Known Exploited Vulnerabilities catalog. Public JSON feed, no key."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

from pydantic import BaseModel, Field

from ..errors import NotFoundError
from ..operation import operation
from ..provider import Provider
from ._common import CveId, utcnow

FEED = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
CACHE_TTL = 6 * 3600


class KEVEntry(BaseModel):
    cve_id: str
    vendor: str
    product: str
    name: str
    date_added: date
    due_date: date | None = None
    description: str = ""
    required_action: str = ""
    ransomware: bool = Field(False, description="Known use in ransomware campaigns")
    cwes: list[str] = []
    notes: str | None = None


class KEVStats(BaseModel):
    catalog_version: str
    released: str
    total: int
    ransomware_known: int
    added_last_30_days: int
    top_vendors: dict[str, int]


class KEV(Provider):
    name = "kev"
    title = "CISA KEV"
    description = "CISA's catalog of vulnerabilities known to be exploited in the wild."
    docs_url = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"

    def _catalog(self) -> dict[str, Any]:
        return self.http.get_json(FEED, cache_ttl=CACHE_TTL, timeout=60)

    def entries(self) -> list[KEVEntry]:
        """All catalog entries (cached). Plain method, used by other providers."""
        return [_parse(v) for v in self._catalog().get("vulnerabilities", [])]

    def index(self) -> dict[str, KEVEntry]:
        return {e.cve_id: e for e in self.entries()}

    @operation(examples=["secctl kev lookup CVE-2023-4966"])
    def lookup(self, cve_id: CveId) -> KEVEntry:
        """Return the KEV entry for a CVE, or a not_found error if it is not listed."""
        entry = self.index().get(cve_id)
        if entry is None:
            raise NotFoundError(f"{cve_id} is not in the CISA KEV catalog")
        return entry

    @operation(
        examples=[
            "secctl kev search --vendor microsoft --days 30",
            "secctl kev search --ransomware",
        ]
    )
    def search(
        self,
        vendor: Annotated[
            str | None, Field(description="Vendor substring (case-insensitive)")
        ] = None,
        product: Annotated[
            str | None, Field(description="Product substring (case-insensitive)")
        ] = None,
        days: Annotated[int | None, Field(ge=1, description="Added in the last N days")] = None,
        ransomware: Annotated[
            bool, Field(description="Only entries with known ransomware use")
        ] = False,
        limit: Annotated[int, Field(ge=1, le=5000)] = 100,
    ) -> list[KEVEntry]:
        """Filter the catalog. Newest additions first."""
        cutoff = (utcnow() - timedelta(days=days)).date() if days else None
        out = []
        for e in self.entries():
            if vendor and vendor.lower() not in e.vendor.lower():
                continue
            if product and product.lower() not in e.product.lower():
                continue
            if cutoff and e.date_added < cutoff:
                continue
            if ransomware and not e.ransomware:
                continue
            out.append(e)
        out.sort(key=lambda e: e.date_added, reverse=True)
        return out[:limit]

    @operation
    def stats(self) -> KEVStats:
        """Catalog summary: size, ransomware share, recent additions, top vendors."""
        cat = self._catalog()
        entries = [_parse(v) for v in cat.get("vulnerabilities", [])]
        cutoff = (utcnow() - timedelta(days=30)).date()
        vendors: dict[str, int] = {}
        for e in entries:
            vendors[e.vendor] = vendors.get(e.vendor, 0) + 1
        top = dict(sorted(vendors.items(), key=lambda kv: kv[1], reverse=True)[:10])
        return KEVStats(
            catalog_version=str(cat.get("catalogVersion", "")),
            released=str(cat.get("dateReleased", "")),
            total=len(entries),
            ransomware_known=sum(e.ransomware for e in entries),
            added_last_30_days=sum(e.date_added >= cutoff for e in entries),
            top_vendors=top,
        )


def _parse(v: dict[str, Any]) -> KEVEntry:
    return KEVEntry(
        cve_id=v["cveID"],
        vendor=v.get("vendorProject", ""),
        product=v.get("product", ""),
        name=v.get("vulnerabilityName", ""),
        date_added=v["dateAdded"],
        due_date=v.get("dueDate") or None,
        description=v.get("shortDescription", ""),
        required_action=v.get("requiredAction", ""),
        ransomware=str(v.get("knownRansomwareCampaignUse", "")).lower() == "known",
        cwes=v.get("cwes") or [],
        notes=v.get("notes") or None,
    )
