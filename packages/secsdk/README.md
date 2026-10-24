# secsdk

Provider-based Python SDK for security data (NVD, CISA KEV, EPSS, passive OSINT, SEC EDGAR,
RSS/Feedly, Tenable, Azure). Typed pydantic inputs/outputs, shared retries/rate limits/caching,
structured errors. See the repository docs for details.

```python
from secsdk import Client

with Client() as sec:
    print(sec.kev.lookup("CVE-2023-4966"))
```
