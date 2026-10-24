# 04 · The model route: where limits bite

*Every model call is counted against you, your team, the agent, the run and the deployment.*

The second API, **`/models`**, is the only way to the model, and only the runtime's identity
may call it. Its policy reads the context headers, checks the agent may use the model, then
applies APIM's `llm-token-limit` policy several times with different *counter keys*. All of
them must pass.

| Counter | Limit comes from (`gateway.yaml`) | Over it |
|---|---|---|
| `u:<you>:<tier>` | your tier's `tokens_per_minute` and `tokens_per_day` | 429, then 403 until 00:00 UTC |
| `u:<you>:month` | your tier's `tokens_per_month` | 403 |
| `t:<team>` | your team's `tokens_per_month` (shared) | 403 |
| `a:<agent>` | the agent's `tokens_per_minute` and `tokens_per_day` | 429 / 403 |
| `r:<run-id>` | the agent's `tokens_per_run` (backstop) | 403 |
| `d:<deployment>` | the deployment's `tokens_per_minute` (all agents) | 429 |

!!! warning "What the pilot taught us"
    Per-minute limits are a token bucket that one big call can overdraw, and parallel runs
    can overshoot it about 2×. Daily and monthly quotas held exactly. The "tokens left"
    headers are estimates that become accurate near the limit.

## Steps

!!! example "Run: read the limits, the source of truth"
    ```bash
    cat deploy/agent-gateway/gateway.example.yaml     # or your own gateway.yaml
    ```

!!! example "Run: see those numbers become policy"
    ```bash
    grep -n "llm-token-limit" -A4 deploy/agent-gateway/policies/models-api.xml | head -40
    ```

!!! example "Run: spend some tokens and watch the counters"
    ```bash
    uv run python deploy/agent-gateway/client.py run cve-triage "Prioritise CVE-2024-3400 and CVE-2023-4966"
    ```
    The last line shows tokens used and **Remaining** per counter. Run it twice within a
    minute on a low tier and you may meet a `429` with a retry time.

!!! info "Look: which limit refused whom"
    Azure portal → Monitor → Workbooks → **Agent gateway** → *Refused requests* names the policy
    (`llm-token-limit[N]`) and reason (`OpenAITokenLimitExceeded`, `OpenAITokenQuotaExceeded`).

??? question "Check: why does the gateway refuse `stream: true`?"
    For streamed responses APIM *estimates* tokens instead of reading the real `usage` block.
    Refusing streams keeps every count exact, which matters when counts are money and limits.

[Next: the agent runtime →](05-runtime.md)
