"""Passive OSINT from public, keyless sources.

Everything here is *passive*: it reads public datasets (certificate transparency,
DNS, RDAP registries, Shodan's free InternetDB). Nothing sends traffic to the
target itself, which keeps it safe to run against anything.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from ..errors import NotFoundError
from ..operation import operation
from ..provider import Provider
from ._common import Domain, IPAddress, parse_dt

CRTSH = "https://crt.sh/"
DOH = "https://cloudflare-dns.com/dns-query"
RDAP = "https://rdap.org"
INTERNETDB = "https://internetdb.shodan.io"

RecordType = Literal["A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA", "CAA", "SRV", "PTR"]
DNS_TYPES = {
    1: "A",
    2: "NS",
    5: "CNAME",
    6: "SOA",
    12: "PTR",
    15: "MX",
    16: "TXT",
    28: "AAAA",
    33: "SRV",
    257: "CAA",
}


# ----- models -------------------------------------------------------------------------


class Subdomain(BaseModel):
    name: str
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    certificates: int = Field(description="Number of CT log entries naming this host")


class DnsRecord(BaseModel):
    name: str
    type: str
    ttl: int | None = None
    value: str


class RdapDomain(BaseModel):
    domain: str
    registrar: str | None = None
    status: list[str] = []
    nameservers: list[str] = []
    registered: datetime | None = None
    expires: datetime | None = None
    updated: datetime | None = None
    dnssec: bool | None = None


class RdapNetwork(BaseModel):
    ip: str
    handle: str | None = None
    name: str | None = None
    range: str | None = None
    country: str | None = None
    organization: str | None = None


class HostExposure(BaseModel):
    ip: str
    known: bool = Field(description="False when Shodan has no data for this IP")
    ports: list[int] = []
    hostnames: list[str] = []
    cpes: list[str] = []
    tags: list[str] = []
    vulns: list[str] = []


class Finding(BaseModel):
    id: str
    severity: Literal["info", "low", "medium", "high"]
    message: str


class EmailSecurity(BaseModel):
    domain: str
    spf: str | None = None
    dmarc: str | None = None
    dmarc_policy: str | None = None
    mx: list[str] = []
    findings: list[Finding] = []


class DomainProfile(BaseModel):
    domain: str
    dns: list[DnsRecord]
    registration: RdapDomain | None = None
    email: EmailSecurity
    subdomains: list[Subdomain] | None = None
    exposure: list[HostExposure] = []


# ----- provider -----------------------------------------------------------------------


class OSINT(Provider):
    name = "osint"
    title = "Passive OSINT"
    description = "Certificate transparency (crt.sh), DNS over HTTPS, RDAP and Shodan InternetDB. No keys, no active scanning."
    rate_limit = (5, 1.0)

    @operation(examples=["secctl osint subdomains example.com"])
    def subdomains(
        self,
        domain: Domain,
        include_expired: Annotated[
            bool, Field(description="Include names only seen on expired certs")
        ] = True,
        limit: Annotated[int, Field(ge=1, le=10000)] = 500,
    ) -> list[Subdomain]:
        """Enumerate hostnames from certificate transparency logs (crt.sh)."""
        params: dict[str, Any] = {"q": f"%.{domain}", "output": "json"}
        if not include_expired:
            params["exclude"] = "expired"
        rows = (
            self.http.get_json(
                CRTSH, params=params, limiter=self.limiter, timeout=90, cache_ttl=3600
            )
            or []
        )
        return aggregate_ct(rows, domain)[:limit]

    @operation(
        examples=[
            "secctl osint dns example.com",
            "secctl osint dns example.com --types MX --types TXT",
        ]
    )
    def dns(
        self,
        name: Domain,
        types: Annotated[list[RecordType], Field(description="Record types to query")] = [
            "A",
            "AAAA",
            "CNAME",
            "MX",
            "NS",
            "TXT",
        ],
    ) -> list[DnsRecord]:
        """Resolve DNS records over HTTPS (Cloudflare DoH)."""
        out: list[DnsRecord] = []
        for rtype in dict.fromkeys(types):
            data = self.http.get_json(
                DOH,
                params={"name": name, "type": rtype},
                headers={"Accept": "application/dns-json"},
                limiter=self.limiter,
            )
            for ans in data.get("Answer", []) or []:
                atype = DNS_TYPES.get(ans.get("type"), str(ans.get("type")))
                if atype != rtype:
                    continue  # skip CNAME chain hops returned for other types
                out.append(
                    DnsRecord(
                        name=ans["name"].rstrip("."),
                        type=atype,
                        ttl=ans.get("TTL"),
                        value=_clean_rdata(ans["data"]),
                    )
                )
        return out

    @operation(examples=["secctl osint whois example.com"])
    def whois(self, domain: Domain) -> RdapDomain:
        """Domain registration data via RDAP (the structured successor to WHOIS)."""
        data = self.http.get_json(
            f"{RDAP}/domain/{domain}",
            headers={"Accept": "application/rdap+json"},
            limiter=self.limiter,
        )
        return parse_rdap_domain(domain, data)

    @operation(examples=["secctl osint ip-owner 8.8.8.8"])
    def ip_owner(self, ip: IPAddress) -> RdapNetwork:
        """Network owner of an IP via RDAP (regional internet registries)."""
        data = self.http.get_json(
            f"{RDAP}/ip/{ip}", headers={"Accept": "application/rdap+json"}, limiter=self.limiter
        )
        org = None
        for ent in data.get("entities", []):
            if "registrant" in ent.get("roles", []) or org is None:
                org = _vcard_name(ent) or org
        start, end = data.get("startAddress"), data.get("endAddress")
        return RdapNetwork(
            ip=ip,
            handle=data.get("handle"),
            name=data.get("name"),
            range=f"{start} - {end}" if start and end else None,
            country=data.get("country"),
            organization=org,
        )

    @operation(examples=["secctl osint exposure 1.1.1.1"])
    def exposure(self, ip: IPAddress) -> HostExposure:
        """Open ports, CPEs and known CVEs for an IP from Shodan InternetDB (free, no key)."""
        try:
            data = self.http.get_json(f"{INTERNETDB}/{ip}", limiter=self.limiter, cache_ttl=3600)
        except NotFoundError:
            return HostExposure(ip=ip, known=False)
        return HostExposure(
            ip=ip,
            known=True,
            ports=sorted(data.get("ports", [])),
            hostnames=data.get("hostnames", []),
            cpes=data.get("cpes", []),
            tags=data.get("tags", []),
            vulns=sorted(data.get("vulns", [])),
        )

    @operation(examples=["secctl osint email-security example.com"])
    def email_security(self, domain: Domain) -> EmailSecurity:
        """Check SPF and DMARC posture and flag weak or missing policies."""
        txt = [r.value for r in self.dns(domain, types=["TXT"])]
        dmarc_txt = [r.value for r in self.dns(f"_dmarc.{domain}", types=["TXT"])]
        mx = [r.value for r in self.dns(domain, types=["MX"])]
        return assess_email(domain, txt, dmarc_txt, mx)

    @operation(
        examples=[
            "secctl osint profile example.com",
            "secctl osint profile example.com --subdomains --exposure",
        ]
    )
    def profile(
        self,
        domain: Domain,
        subdomains: Annotated[bool, Field(description="Include CT subdomains (slow)")] = False,
        exposure: Annotated[
            bool, Field(description="Look up InternetDB exposure for the domain's A records")
        ] = False,
    ) -> DomainProfile:
        """One-shot passive profile: DNS, registration, email security, optional subdomains and exposure."""
        records = self.dns(domain)
        try:
            reg: RdapDomain | None = self.whois(domain)
        except NotFoundError:
            reg = None
        prof = DomainProfile(
            domain=domain, dns=records, registration=reg, email=self.email_security(domain)
        )
        if subdomains:
            prof.subdomains = self.subdomains(domain)
        if exposure:
            ips = sorted({r.value for r in records if r.type in ("A", "AAAA")})
            prof.exposure = [self.exposure(ip) for ip in ips[:10]]
        return prof


# ----- pure helpers (unit-tested directly) ---------------------------------------------


def aggregate_ct(rows: list[dict[str, Any]], domain: str) -> list[Subdomain]:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_names = [*(row.get("name_value") or "").split("\n"), row.get("common_name") or ""]
        names = {
            n.strip().lower().removeprefix("*.").rstrip(".") for n in raw_names
        }  # one count per cert
        nb, na = parse_dt(row.get("not_before")), parse_dt(row.get("not_after"))
        for name in names:
            if not name or not (name == domain or name.endswith("." + domain)):
                continue
            agg = seen.setdefault(name, {"first": nb, "last": na, "count": 0})
            agg["count"] += 1
            if nb and (agg["first"] is None or nb < agg["first"]):
                agg["first"] = nb
            if na and (agg["last"] is None or na > agg["last"]):
                agg["last"] = na
    subs = [
        Subdomain(name=n, first_seen=a["first"], last_seen=a["last"], certificates=a["count"])
        for n, a in seen.items()
    ]
    subs.sort(key=lambda s: s.name)
    return subs


def assess_email(domain: str, txt: list[str], dmarc_txt: list[str], mx: list[str]) -> EmailSecurity:
    findings: list[Finding] = []
    spf_records = [t for t in txt if t.lower().startswith("v=spf1")]
    spf = spf_records[0] if spf_records else None
    if not spf:
        findings.append(
            Finding(
                id="spf-missing",
                severity="medium",
                message="No SPF record; anyone can claim to send as this domain.",
            )
        )
    else:
        if len(spf_records) > 1:
            findings.append(
                Finding(
                    id="spf-multiple",
                    severity="medium",
                    message="Multiple SPF records; receivers treat this as a permanent error.",
                )
            )
        lowered = spf.lower()
        if "+all" in lowered:
            findings.append(
                Finding(
                    id="spf-pass-all",
                    severity="high",
                    message="SPF ends in +all, which authorises every sender.",
                )
            )
        elif "?all" in lowered:
            findings.append(
                Finding(
                    id="spf-neutral",
                    severity="low",
                    message="SPF ends in ?all (neutral); it does not reject spoofed mail.",
                )
            )
        elif not any(q in lowered for q in ("-all", "~all", "redirect=")):
            findings.append(
                Finding(id="spf-no-all", severity="low", message="SPF has no 'all' mechanism.")
            )

    dmarc = next((t for t in dmarc_txt if t.lower().startswith("v=dmarc1")), None)
    policy = None
    if not dmarc:
        findings.append(
            Finding(
                id="dmarc-missing", severity="medium", message="No DMARC record at _dmarc." + domain
            )
        )
    else:
        tags = dict(
            (k.strip().lower(), v.strip().lower())
            for k, _, v in (part.partition("=") for part in dmarc.split(";"))
            if k.strip()
        )
        policy = tags.get("p")
        if policy == "none":
            findings.append(
                Finding(
                    id="dmarc-monitor-only",
                    severity="low",
                    message="DMARC policy is p=none (monitoring only).",
                )
            )
        if "rua" not in tags:
            findings.append(
                Finding(
                    id="dmarc-no-reports",
                    severity="info",
                    message="DMARC has no rua= aggregate report address.",
                )
            )
        pct = tags.get("pct")
        if pct and pct.isdigit() and int(pct) < 100:
            findings.append(
                Finding(
                    id="dmarc-partial",
                    severity="low",
                    message=f"DMARC applies to only {pct}% of mail.",
                )
            )

    if not mx:
        findings.append(
            Finding(
                id="mx-missing",
                severity="info",
                message="No MX records; domain does not receive mail.",
            )
        )
    return EmailSecurity(
        domain=domain, spf=spf, dmarc=dmarc, dmarc_policy=policy, mx=mx, findings=findings
    )


def parse_rdap_domain(domain: str, data: dict[str, Any]) -> RdapDomain:
    events = {e.get("eventAction"): parse_dt(e.get("eventDate")) for e in data.get("events", [])}
    registrar = None
    for ent in data.get("entities", []):
        if "registrar" in ent.get("roles", []):
            registrar = _vcard_name(ent)
    return RdapDomain(
        domain=domain,
        registrar=registrar,
        status=data.get("status", []),
        nameservers=sorted(
            ns.get("ldhName", "").lower() for ns in data.get("nameservers", []) if ns.get("ldhName")
        ),
        registered=events.get("registration"),
        expires=events.get("expiration"),
        updated=events.get("last changed"),
        dnssec=(data.get("secureDNS") or {}).get("delegationSigned"),
    )


def _vcard_name(entity: dict[str, Any]) -> str | None:
    vcard = entity.get("vcardArray")
    if isinstance(vcard, list) and len(vcard) == 2:
        for item in vcard[1]:
            if item and item[0] == "fn":
                return str(item[3])
    return None


def _clean_rdata(value: str) -> str:
    v = value.strip()
    if v.startswith('"') and v.endswith('"'):
        # TXT records arrive as one or more quoted strings: join them
        v = "".join(part for part in v[1:-1].split('" "'))
    return v.rstrip(".").strip() if not v.startswith("v=") else v
