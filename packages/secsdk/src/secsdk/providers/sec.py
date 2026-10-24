"""SEC EDGAR: company lookup, filings, cybersecurity disclosures and filing text.

No API key, but the SEC requires a descriptive User-Agent with a contact email
(https://www.sec.gov/os/accessing-edgar-data). Set ``SECSDK_SEC_USER_AGENT``,
e.g. ``"Acme Security Research security@acme.example"``.

Cyber-relevant disclosures:

* **8-K Item 1.05**: material cybersecurity incidents (since Dec 2023).
* **10-K Item 1C**: cybersecurity risk management and governance.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Annotated, Any
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import SettingsConfigDict

from ..config import ProviderSettings
from ..errors import InvalidInputError, NotFoundError
from ..operation import operation
from ..provider import AuthStatus, Provider
from ._common import html_to_text, utcnow

TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EFTS = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
ALLOWED_HOSTS = {"www.sec.gov", "sec.gov", "data.sec.gov"}


class SECSettings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="SECSDK_SEC_")
    user_agent: str | None = Field(
        None,
        validation_alias=AliasChoices("SECSDK_SEC_USER_AGENT", "SEC_USER_AGENT"),
        description="'<Org or name> <contact email>' as required by SEC fair-access policy",
    )


class Company(BaseModel):
    cik: int
    ticker: str
    name: str


class Filing(BaseModel):
    cik: int
    company: str
    form: str
    filed: date
    accession: str
    items: list[str] = Field(default=[], description="8-K item numbers, e.g. ['1.05', '9.01']")
    description: str | None = None
    url: str


class FilingText(BaseModel):
    url: str
    section: str | None = None
    found: bool = True
    chars: int
    truncated: bool
    text: str


class SEC(Provider):
    name = "sec"
    title = "SEC EDGAR"
    description = "Company lookup, filing history, 8-K Item 1.05 cyber incidents and filing text from SEC EDGAR."
    docs_url = "https://www.sec.gov/search-filings/edgar-application-programming-interfaces"
    settings_class = SECSettings
    settings: SECSettings
    rate_limit = (8, 1.0)  # SEC allows 10 req/s; stay under it

    def auth_status(self) -> AuthStatus:
        ok = bool(self.settings.user_agent and "@" in self.settings.user_agent)
        return AuthStatus(
            required=True,
            configured=ok,
            missing=[] if ok else ["SECSDK_SEC_USER_AGENT"],
            note="No key needed. SEC requires a User-Agent with contact email, e.g. "
            "SECSDK_SEC_USER_AGENT='Acme Research security@acme.example'.",
        )

    def _get(self, url: str, **kw: Any) -> Any:
        headers = {"User-Agent": self.settings.user_agent or "", "Accept-Encoding": "gzip, deflate"}
        return self.http.request("GET", url, headers=headers, limiter=self.limiter, **kw)

    def _json(self, url: str, **kw: Any) -> Any:
        from ..http import parse_json

        return parse_json(self._get(url, **kw))

    # ----- companies -------------------------------------------------------------

    def _companies(self) -> list[Company]:
        data = self._json(TICKERS, cache_ttl=24 * 3600)
        return [
            Company(cik=v["cik_str"], ticker=v["ticker"], name=v["title"]) for v in data.values()
        ]

    def resolve(self, company: str) -> Company:
        """Ticker, CIK or exact name to a Company."""
        q = company.strip()
        companies = self._companies()
        if q.isdigit():
            cik = int(q)
            return next(
                (c for c in companies if c.cik == cik), Company(cik=cik, ticker="", name="")
            )
        up = q.upper()
        for c in companies:
            if c.ticker.upper() == up or c.name.upper() == up:
                return c
        raise NotFoundError(
            f"No SEC registrant matches '{company}'", hint="Try `secctl sec lookup <name>`."
        )

    @operation(auth=True, examples=["secctl sec lookup okta", "secctl sec lookup MSFT"])
    def lookup(
        self,
        query: Annotated[
            str, Field(min_length=1, description="Ticker, CIK or part of the company name")
        ],
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
    ) -> list[Company]:
        """Find SEC registrants by ticker, CIK or name."""
        q = query.strip().upper()
        companies = self._companies()
        exact = [c for c in companies if c.ticker.upper() == q or str(c.cik) == q.lstrip("0")]
        partial = [c for c in companies if q in c.name.upper() and c not in exact]
        return (exact + partial)[:limit]

    # ----- filings ---------------------------------------------------------------

    @operation(
        auth=True,
        examples=[
            "secctl sec filings MSFT --forms 8-K --days 365",
            "secctl sec filings MSFT --item 1.05",
            "secctl sec filings CRWD --forms 10-K --limit 1",
        ],
    )
    def filings(
        self,
        company: Annotated[str, Field(description="Ticker or CIK")],
        forms: Annotated[
            list[str], Field(description="Form types to include, e.g. 8-K, 10-K")
        ] = [],
        item: Annotated[
            str | None, Field(description="Only 8-Ks reporting this item, e.g. 1.05")
        ] = None,
        days: Annotated[int | None, Field(ge=1, description="Filed in the last N days")] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 25,
    ) -> list[Filing]:
        """Recent filings for a company, filterable by form, 8-K item and age."""
        co = self.resolve(company)
        data = self._json(SUBMISSIONS.format(cik=co.cik))
        name = data.get("name") or co.name
        filings = parse_recent(co.cik, name, data.get("filings", {}).get("recent", {}))
        wanted = {f.upper() for f in forms}
        cutoff = utcnow().date() - timedelta(days=days) if days else None
        out = [
            f
            for f in filings
            if (not wanted or f.form.upper() in wanted)
            and (not item or item in f.items)
            and (not cutoff or f.filed >= cutoff)
        ]
        return out[:limit]

    @operation(auth=True, examples=["secctl sec cyber-incidents --days 90"])
    def cyber_incidents(
        self,
        days: Annotated[int, Field(ge=1, le=3650, description="Look back N days")] = 90,
        query: Annotated[str, Field(description="EDGAR full-text query")] = '"Item 1.05"',
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> list[Filing]:
        """8-K filings disclosing material cybersecurity incidents (Item 1.05), via EDGAR full-text search."""
        end = utcnow().date()
        start = end - timedelta(days=days)
        out: list[Filing] = []
        seen: set[str] = set()
        offset = 0
        while len(out) < limit:
            data = self._json(
                EFTS,
                params={
                    "q": query,
                    "forms": "8-K,8-K/A",
                    "dateRange": "custom",
                    "startdt": start.isoformat(),
                    "enddt": end.isoformat(),
                    "from": offset,
                },
            )
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                f = parse_efts_hit(hit)
                if f is None or f.accession in seen:
                    continue
                if f.items and "1.05" not in f.items:
                    continue
                seen.add(f.accession)
                out.append(f)
            offset += len(hits)
            if offset >= int(data.get("hits", {}).get("total", {}).get("value", 0)):
                break
        out.sort(key=lambda f: f.filed, reverse=True)
        return out[:limit]

    @operation(
        auth=True,
        examples=[
            "secctl sec document <url-from-filings> --section 1.05",
            "secctl sec document <10-K url> --section 1C --max-chars 8000",
        ],
    )
    def document(
        self,
        url: Annotated[
            str, Field(description="Filing document URL on sec.gov (from `sec filings`)")
        ],
        section: Annotated[
            str | None, Field(description="Extract one item, e.g. '1.05' (8-K) or '1C' (10-K)")
        ] = None,
        max_chars: Annotated[int, Field(ge=500, le=200_000)] = 20_000,
    ) -> FilingText:
        """Fetch a filing document as plain text, optionally just one Item section."""
        host = urlparse(url).hostname or ""
        if host not in ALLOWED_HOSTS:
            raise InvalidInputError("Only sec.gov URLs are allowed", details={"host": host})
        text = html_to_text(self._get(url, cache_ttl=24 * 3600).text)
        found = True
        if section:
            extracted = extract_item(text, section)
            found = extracted is not None
            text = extracted or ""
        return FilingText(
            url=url,
            section=section,
            found=found,
            chars=len(text),
            truncated=len(text) > max_chars,
            text=text[:max_chars],
        )


# ----- pure helpers --------------------------------------------------------------------


def parse_recent(cik: int, company: str, recent: dict[str, list[Any]]) -> list[Filing]:
    out = []
    n = len(recent.get("accessionNumber", []))
    for i in range(n):
        acc = recent["accessionNumber"][i]
        doc = recent.get("primaryDocument", [""] * n)[i]
        items_raw = (recent.get("items") or [""] * n)[i] or ""
        out.append(
            Filing(
                cik=cik,
                company=company,
                form=recent["form"][i],
                filed=recent["filingDate"][i],
                accession=acc,
                items=[x.strip() for x in items_raw.split(",") if x.strip()],
                description=(recent.get("primaryDocDescription") or [None] * n)[i] or None,
                url=ARCHIVE.format(cik=cik, acc=acc.replace("-", ""), doc=doc),
            )
        )
    return out


def parse_efts_hit(hit: dict[str, Any]) -> Filing | None:
    src = hit.get("_source", {})
    adsh, _, doc = str(hit.get("_id", "")).partition(":")
    adsh = src.get("adsh") or adsh
    ciks = src.get("ciks") or []
    if not adsh or not ciks:
        return None
    cik = int(ciks[0])
    names = src.get("display_names") or [""]
    company = re.sub(r"\s+", " ", re.sub(r"\s*\(CIK \d+\)\s*$", "", names[0])).strip()
    return Filing(
        cik=cik,
        company=company,
        form=src.get("form") or src.get("file_type") or "8-K",
        filed=src.get("file_date"),
        accession=adsh,
        items=[str(i) for i in src.get("items") or []],
        description=src.get("file_description"),
        url=ARCHIVE.format(cik=cik, acc=adsh.replace("-", ""), doc=doc),
    )


_ITEM_HEAD = re.compile(r"(?im)^\s*item\s*(\d{1,2}[a-z]?(?:\.\d{2})?)\b[.:\s]")


def extract_item(text: str, item: str) -> str | None:
    """Return the text of ``Item <item>`` up to the next Item heading.

    Filings often repeat item headings in a table of contents, so we take the
    occurrence with the longest body, which is the real section.
    """
    target = item.strip().upper().removeprefix("ITEM").strip()
    heads = [(m.start(), m.group(1).upper()) for m in _ITEM_HEAD.finditer(text)]
    best: str | None = None
    for idx, (pos, label) in enumerate(heads):
        if label != target:
            continue
        end = next((p for p, lbl in heads[idx + 1 :] if lbl != target), len(text))
        body = text[pos:end].strip()
        if best is None or len(body) > len(best):
            best = body
    return best
