---
name: domain-posture
description: Passive external posture review of one domain (DNS, registration, email security, exposure).
inputs:
  domain:
    description: Domain you are responsible for, e.g. example.com
  subdomains:
    description: Include certificate-transparency subdomains (slower) - yes or no
    default: "no"
max_turns: 25
required_commands:
  - osint profile
  - osint email-security
  - osint exposure
---
Review the external posture of **{domain}** using passive sources only.
Include subdomains from certificate transparency: **{subdomains}**.

Follow these steps in order.

1. **Orient.** Run `secctl guide` once to learn the osint commands.
2. **Profile.** Run `secctl osint profile {domain}` (add `--subdomains` only if subdomains is "yes").
   Record registrar, expiry date, DNSSEC and nameservers.
3. **Email security.** Run `secctl osint email-security {domain}`. Report every finding with its
   severity exactly as secctl returns it.
4. **Exposure.** For each distinct A record IP from the profile (at most 5), run
   `secctl osint exposure <ip>`. Record open ports and any CVEs InternetDB lists.
5. **Exploited CVEs.** If step 4 found CVEs, run `secctl intel cve-priority <cves> --no-nvd` and
   include only the P1 and P2 results.
6. **Ownership.** For the first A record IP, run `secctl osint ip-owner <ip>` to name the hosting network.
7. **Report.** Write the report below.

## Report format

```
# External posture: {domain}

## Summary
<3 bullets: the most important issues, most severe first>

## Registration
## Email authentication (SPF / DMARC findings table: id, severity, message)
## Internet exposure (table: IP, owner, open ports, CVEs)
## Recommended fixes (ordered; each tied to a finding above)
## Commands run
```

If the domain expires within 60 days, say so in the summary.
