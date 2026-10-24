"""Remote MCP server for secsdk on Azure Functions (MCP extension).

Endpoint once deployed: https://<app>.azurewebsites.net/runtime/webhooks/mcp

Tool names come from the function names, descriptions from the docstrings, and
parameter types from the type hints (azure-functions >= 2.3). The logic lives in
mcp_tools.py so it can be tested without the Functions runtime.

Telemetry: with "telemetryMode": "OpenTelemetry" in host.json and the app setting
PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY=true, the platform configures Azure
Monitor and secsdk's spans and metrics flow into Application Insights automatically.
"""

import azure.functions as func

import mcp_tools as tools

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


# ----- discovery (same verbs as the secctl CLI) ---------------------------------------------


@app.mcp_tool()
def secsdk_guide() -> str:
    """Start here. Lists every security data operation on this server, its parameters, and how to call it with secsdk_call."""
    return tools.guide()


@app.mcp_tool()
def secsdk_providers() -> str:
    """Lists data providers and whether each has credentials configured on this server. Skip providers with configured=false."""
    return tools.providers()


@app.mcp_tool()
@app.mcp_tool_property(
    arg_name="operation", description="Operation name, e.g. 'kev.lookup' or 'osint.email_security'."
)
def secsdk_describe(operation: str) -> str:
    """Full parameter schema and examples for one operation."""
    return tools.describe(operation)


@app.mcp_tool()
@app.mcp_tool_property(
    arg_name="operation", description="Operation name from secsdk_guide, e.g. 'kev.search'."
)
@app.mcp_tool_property(
    arg_name="params",
    description='JSON object of parameters, e.g. {"vendor": "citrix", "days": 30}. Use {} when there are none.',
    is_required=False,
)
def secsdk_call(operation: str, params: str = "{}") -> str:
    """Run any read-only security data operation (vulnerability intel, passive OSINT, SEC filings, threat news, Tenable, Azure). Returns a JSON envelope: check `ok`, and on failure follow `error.hint`."""
    return tools.call(operation, params)


# ----- shortcuts for the most common tasks ---------------------------------------------------


@app.mcp_tool()
@app.mcp_tool_property(
    arg_name="cve_ids",
    description="CVE ids, e.g. ['CVE-2024-3400', 'CVE-2023-4966'].",
    as_array=True,
)
@app.mcp_tool_property(
    arg_name="use_nvd", description="Also fetch CVSS from NVD (slower).", is_required=False
)
def intel_cve_priority(cve_ids: list[str], use_nvd: bool = False) -> str:
    """Prioritise CVEs P1-P4 using CISA KEV, EPSS and optionally NVD CVSS. The `reasons` field explains each priority; do not re-score."""
    return tools.call("intel.cve_priority", {"cve_ids": cve_ids, "nvd": use_nvd})


@app.mcp_tool()
@app.mcp_tool_property(arg_name="cve_id", description="CVE id, e.g. CVE-2023-4966.")
def kev_lookup(cve_id: str) -> str:
    """Is this CVE in CISA's Known Exploited Vulnerabilities catalog? Returns due date, ransomware use and required action, or not_found."""
    return tools.call("kev.lookup", {"cve_id": cve_id})


@app.mcp_tool()
@app.mcp_tool_property(arg_name="domain", description="Domain to profile, e.g. example.com.")
def osint_profile(domain: str) -> str:
    """Passive domain profile: DNS records, registration (RDAP), and SPF/DMARC findings. Never contacts the target."""
    return tools.call("osint.profile", {"domain": domain})


@app.mcp_tool()
@app.mcp_tool_property(
    arg_name="days", description="Look-back window in days (1-60).", is_required=False
)
@app.mcp_tool_property(
    arg_name="keyword", description="Optional keyword filter, e.g. 'ransomware'.", is_required=False
)
def feeds_news(days: int = 2, keyword: str = "") -> str:
    """Recent security news and advisories (CISA, Krebs, SANS ISC, ...), de-duplicated, with CVE ids extracted."""
    params: dict = {"days": days, "limit": 40}
    if keyword:
        params["keyword"] = keyword
    return tools.call("feeds.news", params)
