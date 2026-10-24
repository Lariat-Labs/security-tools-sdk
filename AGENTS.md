# AGENTS.md: using `secctl`

You answer security questions using the `secctl` CLI. It is your only interface to
security data. Do not import `secsdk` directly, and do not call the upstream APIs yourself.

## Start of every session

```bash
uv run secctl guide        # every command, one line each, plus conventions
uv run secctl providers    # what is configured; skip providers with configured=false
```

## Rules

1. Output is a JSON envelope. Check `ok` first.
2. On `ok: false`, act on `error.code`:
   - `invalid_input`: fix the arguments; `error.details` names the parameter.
   - `not_configured`: stop and tell the user which variable is missing (`error.hint`).
   - `upstream_error` / `rate_limited`: retry at most once, then report.
   - `not_found`: a valid answer (e.g. "not in KEV"), not a failure.
3. Use `secctl describe <provider>.<operation>` before using an unfamiliar operation.
4. Keep output small: `--fields a,b`, `--compact`, and `--limit` where available.
5. Scores and priorities come from secctl (`intel cve-priority`, `osint email-security`
   findings). Explain them using the `reasons`/`findings` fields. Don't invent your own scoring.
6. All operations are read-only and passive. Do not run scanners or other network
   tools against targets.
7. Cite where the data came from (provider name and, for filings or articles, the URL).

## Useful starting points

| Question | Command |
|---|---|
| Is this CVE exploited / how urgent? | `secctl intel cve-priority CVE-… [CVE-…]` |
| What changed in KEV recently? | `secctl kev search --days 7` |
| What does this domain expose? | `secctl osint profile example.com --subdomains --exposure` |
| Who disclosed cyber incidents? | `secctl sec cyber-incidents --days 30` |
| Read a company's cyber governance | `secctl sec filings TICKER --forms 10-K --limit 1` → `secctl sec document <url> --section 1C` |
| Security news today | `secctl feeds news --days 1 --fields source,title,url,cves` |
| Our KEV exposure (Tenable) | `secctl intel kev-exposure` |
| Our Azure attack surface | `secctl azure check public_ips` / `open_management_ports` |
