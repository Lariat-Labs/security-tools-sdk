# Providers

<!-- secsdk:providers -->

Full parameter tables for every operation are in the
[operations reference](reference/operations.md). Notes on each source follow.

## Vulnerability intelligence

**`nvd`** queries the [NVD CVE API 2.0](https://nvd.nist.gov/developers/vulnerabilities).
Returns the highest-version CVSS score available (4.0 → 3.1 → 3.0 → 2.0), CWEs, and
NVD's own KEV flag. Without a key, NVD allows 5 requests per 30 seconds, and the SDK
enforces that limit, so large batches are slow by design.

**`kev`** reads the [CISA Known Exploited Vulnerabilities](https://www.cisa.gov/known-exploited-vulnerabilities-catalog)
catalog. The whole catalog is one JSON file, cached for 6 hours, so `lookup` and
`search` are effectively instant after the first call.

**`epss`** queries [FIRST EPSS](https://www.first.org/epss/): the probability that a CVE sees
exploitation activity in the next 30 days. Batches up to 100 CVEs per request.

**`intel`** holds cross-source rules. `cve_priority` combines KEV, EPSS and (optionally)
NVD CVSS using the documented [`score_cve`](reference/python-api.md#secsdk.providers.intel.score_cve)
rules. `kev_exposure` intersects your Tenable findings with KEV.

## OSINT (passive)

**`osint`** uses only third-party datasets and never contacts the target:

| Operation | Source |
|---|---|
| `subdomains` | Certificate transparency via [crt.sh](https://crt.sh) (can be slow, cached 1h) |
| `dns` | Cloudflare DNS-over-HTTPS |
| `whois`, `ip_owner` | RDAP via [rdap.org](https://rdap.org) bootstrap |
| `exposure` | [Shodan InternetDB](https://internetdb.shodan.io), free, no key |
| `email_security` | DNS TXT records, scored by deterministic SPF/DMARC rules |
| `profile` | All of the above in one call |

## Filings and news

**`sec`** queries SEC EDGAR. Highlights for security work:

- `cyber_incidents`: 8-K filings with **Item 1.05** (material cybersecurity incidents),
  found with EDGAR full-text search.
- `filings <ticker> --item 1.05`: the same, for one company.
- `document <url> --section 1C`: the **10-K Item 1C** cybersecurity governance
  section as plain text. Section extraction skips the table of contents.

**`feeds`** reads RSS/Atom with [`defusedxml`](https://pypi.org/project/defusedxml/), because feeds are untrusted XML.
Ships a curated list (CISA advisories, CISA news, Krebs, BleepingComputer, The Hacker News, SANS ISC).
`news` merges and de-duplicates the feeds. A feed that fails shows up in `errors`
without failing the whole digest.

**`feedly`** uses the [Feedly Cloud API](https://developers.feedly.com/). `search` (feed discovery) is
keyless. `collections` and `stream` need a token. Threat Intelligence plans return
`entities` (actors, malware, CVEs) on articles.

## Your environment

**`tenable`** connects to Tenable Vulnerability Management. `findings` uses the
asynchronous **export API**: it starts an export, polls, and streams chunks, so it
works for large estates. `summary` is the faster workbench view.

**`azure`** runs [Azure Resource Graph](https://learn.microsoft.com/azure/governance/resource-graph/)
KQL queries. Besides arbitrary `query`, `check` runs reviewed security presets:

| Preset | Finds |
|---|---|
| `public_ips` | Every public IP and what it is attached to |
| `open_management_ports` | NSG rules allowing SSH/RDP/SMB/DB ports from the internet |
| `storage_weak_config` | Storage accounts allowing public blob access, HTTP, TLS < 1.2, open network, shared keys |
| `defender_recommendations` | Unhealthy Defender for Cloud assessments, by severity |

`secctl azure presets` prints the KQL so you can review it.
