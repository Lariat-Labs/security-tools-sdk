# Architecture

## Layers

```mermaid
flowchart TB
    subgraph L4["4 · Faces (optional)"]
        UI["Chat UI / dashboard"]
    end
    subgraph L3["3 · Agent"]
        AG["LLM agent<br/>decides which command to run<br/>and explains the result"]
    end
    subgraph L2["2 · CLI: secctl"]
        CLI["Generated commands<br/>JSON envelope · exit codes<br/>describe / schema / guide"]
    end
    subgraph L1["1 · SDK: secsdk"]
        CL["Client"] --> REG["Registry"]
        REG --> P1["nvd · kev · epss"]
        REG --> P2["osint · feeds · sec"]
        REG --> P3["feedly · tenable · azure"]
        REG --> P4["intel (cross-source rules)"]
        P1 & P2 & P3 & P4 --> HTTP["HttpClient<br/>retries · rate limits · cache · error mapping"]
    end
    UI --> AG
    UI -. "tables without an LLM" .-> CL
    AG -- "shell: secctl …" --> CLI
    CLI --> CL
    HTTP --> EXT[("Public APIs<br/>and your tenants")]
```

| Layer | Contains | Testable without an LLM? |
|---|---|---|
| **SDK** | All fetching, parsing, validation, scoring | Yes, unit tests with mocked HTTP |
| **CLI** | Argument parsing, output formatting, exit codes | Yes, CLI contract tests |
| **Agent** | Choosing commands, judgement, explanation | Run the same CLI commands by hand |
| **Face** | Presentation | Swap it out at any time |

**Rule of thumb:** if a step has a correct answer, it belongs in the SDK. If it
needs judgement ("which of these should we fix first, and why?"), it belongs to
the agent.

`intel.cve_priority` is an example. The *scoring rules* (KEV → P1, EPSS ≥ 0.5 → P2, …)
are a pure function in the SDK with a test for each rule. The agent never
computes priority itself. It explains what the SDK returned.

## Inside the SDK

### Operations

An **operation** is a provider method decorated with `@operation`:

```python
class KEV(Provider):
    name = "kev"

    @operation(examples=["secctl kev lookup CVE-2023-4966"])
    def lookup(self, cve_id: CveId) -> KEVEntry:
        """Return the KEV entry for a CVE."""
```

From that one declaration the SDK derives:

- a **pydantic input model** built from the signature, used to validate every call
  the same way, whether it comes from Python, the CLI or an agent;
- a **name**, `kev.lookup`, used by `client.call(...)`, `secctl call ...` and tool schemas;
- **CLI help** (docstring), **examples**, a **JSON schema**, and an entry in the
  generated [operations reference](reference/operations.md).

### Request lifecycle

```mermaid
sequenceDiagram
    participant C as Caller (CLI / Python / agent)
    participant Cl as Client
    participant P as Provider
    participant H as HttpClient
    participant U as Upstream API
    C->>Cl: execute("kev.lookup", {"cve_id": "cve-2023-4966"})
    Cl->>P: invoke("lookup", params)
    P->>P: validate with input model (normalises to CVE-2023-4966)
    P->>P: auth check (if operation needs credentials)
    P->>H: get_json(FEED, cache_ttl=6h)
    H->>H: cache hit? rate-limit slot?
    H->>U: GET (retry 429/5xx with backoff)
    U-->>H: JSON
    H-->>P: data (or AuthError / NotFoundError / UpstreamError)
    P-->>Cl: KEVEntry (pydantic model)
    Cl-->>C: Result{ok, operation, data, meta}
```

### Errors

Every failure is a subclass of `SecSDKError` with a stable `code` and an `exit_code`:

| Exception | `code` | Exit | Typical cause |
|---|---|---|---|
| `InvalidInputError` | `invalid_input` | 2 | Bad CVE id, bad domain, unknown parameter |
| `UnknownOperationError` | `unknown_operation` | 2 | Typo in operation name |
| `ConfigError` | `not_configured` | 3 | Missing API key or settings |
| `AuthError` | `auth_failed` | 3 | Upstream returned 401/403 |
| `UpstreamError` | `upstream_error` | 4 | 5xx, bad payload, network failure |
| `RateLimitedError` | `rate_limited` | 4 | Still 429 after retries |
| `NotFoundError` | `not_found` | 5 | CVE not in KEV, unknown domain |

Errors carry a `hint` (what to do next) wherever one is known. Agents use hints well.

### Shared HTTP behaviour

All providers go through `secsdk.http.HttpClient`, so every source gets:

- retries with exponential backoff on 429/5xx and network errors (honours `Retry-After`);
- **client-side rate limits** per provider, matching each API's published limits
  (NVD 5 per 30s without a key, SEC 10/s, Resource Graph 15 per 5s, …);
- an opt-in **file cache** for public, slow-changing data (KEV catalog, SEC ticker map,
  CT results). Authenticated responses are never cached;
- mapping of HTTP status codes to the error types above.

### Discovery

`Registry` loads providers from two places:

1. every public module in `secsdk/providers/` (built-ins), and
2. installed packages exposing the `secsdk.providers` entry-point group (plugins).

The CLI, docs and tool schemas all read the registry, so **adding a provider
module is the only step needed** to make it available everywhere. See
[Extending the SDK](extending.md).
