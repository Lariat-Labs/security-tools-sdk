# 06 · Models and quota

*The capacity everyone shares, and why nobody can reach it directly.*

The model lives in an AI Services (Foundry) account as a *deployment*: by default
`gpt-5-mini`, GlobalStandard, 500K tokens per minute, carved out of your region's quota.
**Key authentication is disabled**, and only APIM's managed identity has the *Cognitive
Services OpenAI User* role. So the gateway is the only door.

The gateway's own ceiling for that deployment (`deployments[].tokens_per_minute` in
`gateway.yaml`) is set lower than the deployment, so APIM says no politely before Azure
OpenAI starts throttling everyone.

## Steps

!!! example "Run: the deployment and its capacity"
    ```bash
    az cognitiveservices account deployment list -g $RG -n $AIS \
      --query "[].{name:name, model:properties.model.name, version:properties.model.version, ktpm:sku.capacity}" -o table
    ```

!!! example "Run: your regional quota"
    ```bash
    az cognitiveservices usage list -l $(az group show -n $RG --query location -o tsv) \
      --query "[?contains(name.value,'gpt-5-mini')].{name:name.value, limit:limit, used:currentValue}" -o table
    ```

!!! example "Run: try the model directly"
    ```bash
    curl -s -w "\nHTTP %{http_code}\n" -X POST https://$AIS.openai.azure.com/openai/v1/chat/completions \
      -H "api-key: anything" -H "content-type: application/json" \
      -d '{"model":"gpt-5-mini","messages":[{"role":"user","content":"hi"}]}'
    ```
    `HTTP 401`: keys are switched off.

!!! info "Look: the account and its project"
    `portal "$AIS"` for the Azure resource, and [ai.azure.com](https://ai.azure.com) for the
    Foundry project **agents** (the trace view of module 09).

??? question "Check: users' per-minute limits add up to more than the deployment's. Is that a bug?"
    No, that's deliberate overbooking: few people peak at the same moment. Watch the
    deployment's 429 rate in the overview workbook and add capacity when it climbs.

[Next: OBO and the web chat →](07-obo.md)
