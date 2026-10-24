# Responsible use

This project is for **defensive** security work: vulnerability prioritisation,
attack-surface awareness for organisations you are responsible for, threat news,
and public disclosure research.

## What the tooling does and doesn't do

- **Read-only.** No operation changes anything in Tenable, Azure, Feedly or anywhere else.
- **Passive OSINT.** `osint` reads third-party datasets (CT logs, DNS resolvers,
  RDAP registries, Shodan InternetDB). It never scans, probes or connects to the target itself.
- **No exploitation.** There are no exploit, brute-force or evasion capabilities, and
  contributions adding them will not be accepted.

## Be a good API citizen

- The SDK enforces each source's published rate limits. Leave `SECSDK_RATE_LIMIT` on.
- Set a truthful `SECSDK_SEC_USER_AGENT`. The SEC blocks anonymous or abusive clients.
- Respect each data source's terms of service, especially for redistribution of
  data (e.g. EPSS, Shodan InternetDB and Feedly content).

## Credentials

- Use dedicated, least-privilege, read-only identities (see [Configuration](configuration.md#least-privilege)).
- Keep secrets in environment variables or a secret manager. `.env` is git-ignored.
- When an agent runs `secctl`, it inherits the shell's environment. Give agents
  credentials scoped to what you want them to see.

## Reporting a vulnerability in this project

Please open a private security advisory on GitHub rather than a public issue.
