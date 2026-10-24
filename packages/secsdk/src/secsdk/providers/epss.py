"""FIRST Exploit Prediction Scoring System. Public API, no key."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field

from ..operation import operation
from ..provider import Provider
from ._common import CveId

API = "https://api.first.org/data/v1/epss"
BATCH = 100  # keep query strings well under URL limits


class EPSSScore(BaseModel):
    cve_id: str
    epss: float = Field(
        description="Probability of exploitation activity in the next 30 days (0-1)"
    )
    percentile: float = Field(description="Rank relative to all scored CVEs (0-1)")
    as_of: date = Field(description="Date the score was computed")


class EPSS(Provider):
    name = "epss"
    title = "FIRST EPSS"
    description = "Daily probability that a CVE will see exploitation activity in the next 30 days."
    docs_url = "https://www.first.org/epss/api"
    rate_limit = (60, 60.0)

    @operation(examples=["secctl epss score CVE-2021-44228 CVE-2023-4966"])
    def score(
        self,
        cve_ids: Annotated[
            list[CveId], Field(min_length=1, max_length=1000, description="One or more CVE ids")
        ],
    ) -> list[EPSSScore]:
        """EPSS scores for one or more CVEs. CVEs without a score are omitted."""
        out: list[EPSSScore] = []
        unique = list(dict.fromkeys(cve_ids))
        for i in range(0, len(unique), BATCH):
            chunk = unique[i : i + BATCH]
            data = self.http.get_json(API, params={"cve": ",".join(chunk)}, limiter=self.limiter)
            out.extend(_parse(row) for row in data.get("data", []))
        return out

    @operation(examples=["secctl epss top --limit 20"])
    def top(
        self,
        limit: Annotated[int, Field(ge=1, le=1000)] = 25,
        min_percentile: Annotated[float | None, Field(ge=0, le=1)] = None,
    ) -> list[EPSSScore]:
        """Highest-scoring CVEs today."""
        params = {"order": "!epss", "limit": limit, "percentile-gt": min_percentile}
        data = self.http.get_json(API, params=params, limiter=self.limiter)
        return [_parse(row) for row in data.get("data", [])]


def _parse(row: dict) -> EPSSScore:
    return EPSSScore(
        cve_id=row["cve"],
        epss=float(row["epss"]),
        percentile=float(row["percentile"]),
        as_of=row["date"],
    )
