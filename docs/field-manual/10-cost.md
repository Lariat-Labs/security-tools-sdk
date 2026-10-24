# 10 · Cost and chargeback

*Turning tokens into money per person, team and cost center.*

Each team in `gateway.yaml` has a numeric cost center. The gateway stamps it on every
request, usage event and metric. Cost is tokens times price, split three ways because they're
billed differently: uncached input, cached input (cheaper), and output (dearer). Reasoning
tokens are part of output.

## Steps

!!! info "Look: cost per user and per cost center"
    Azure portal → Monitor → Workbooks → **Agent gateway**. Change the price parameters at the
    top and watch the bars move. Prices default to gpt-5-mini list prices; check your Azure
    price sheet.

!!! example "Run: usage events arriving in Event Hubs"
    ```bash
    az monitor metrics list --resource $(az eventhubs namespace show -g $RG -n $EVHNS --query id -o tsv) \
      --metric IncomingMessages --interval PT1H --aggregation Total -o table
    ```
    One message per model call. In production these flow to a data lake (Event Hubs Capture
    to ADLS) or Fabric for monthly chargeback.

!!! example "Run: what one usage event contains"
    ```bash
    grep -o 'new JProperty("[a-zA-Z]*"' deploy/agent-gateway/policies/models-api.xml | sort -u
    ```

??? question "Check: three places count tokens (metrics, usage events, spans). Which is the bill?"
    Usage events: one record per model call with user, team, cost center, agent and exact
    prompt, cached and completion tokens, written by the gateway. Metrics are for dashboards;
    spans are for debugging and the audit trail.

[Next: infrastructure as code →](11-iac.md)
