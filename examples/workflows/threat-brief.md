---
name: threat-brief
description: Short threat brief from recent news, KEV additions and SEC cyber-incident filings.
inputs:
  days:
    description: Look-back window in days
    default: "3"
max_turns: 30
required_commands:
  - feeds news
  - kev search
  - intel cve-priority
---
Produce a threat brief covering the last **{days}** days.

Follow these steps in order.

1. **Orient.** Run `secctl providers` to see what is configured.
2. **News.** Run `secctl feeds news --days {days} --fields source,title,url,published,cves --limit 40`.
   Pick the 5 most significant stories for a security team. Collect every CVE id they mention.
3. **New KEV entries.** Run `secctl kev search --days {days}`. These are newly confirmed
   exploited vulnerabilities.
4. **Prioritise.** Run `secctl intel cve-priority --no-nvd` with the union of CVEs from steps 2
   and 3 (at most 30). If there are none, run it on the 3 most recent KEV CVEs instead.
5. **Disclosures.** If the `sec` provider is configured, run
   `secctl sec cyber-incidents --days {days} --fields company,filed,form,url`. If it is not
   configured, say so in the report and skip this step.
6. **Report.** Write the report below.

## Report format

```
# Threat brief: last {days} days

## Top stories (5 bullets: headline, one-sentence why it matters, link)
## Newly exploited (KEV additions table: CVE, vendor/product, date added, ransomware)
## Patch now (P1/P2 from intel cve-priority, with reasons)
## Disclosed incidents (SEC 8-K Item 1.05 filings, or why this section is empty)
## Commands run
```

Keep it under one page. Link every story and filing.
