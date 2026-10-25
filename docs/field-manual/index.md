# Agent gateway field manual

A hands-on walkthrough of the [agent gateway](../agent-gateway.md), one piece at a time.
It follows a request in the order it travels: identity, the APIM front door, token limits,
the agent runtime, the models, on-behalf-of (OBO) clients, Teams, observability, cost, and
the infrastructure as code that builds it all.

Each module has the same shape:

- **What and why**, in plain language, with the terms you need defined.
- **A diagram** of the piece.
- **Run** steps: commands for your terminal (copy button top right of each block).
- **Look** steps: where to see the same thing in the Azure portal or a browser.
- **A self-check** question with the answer folded underneath.

It takes about three hours end to end. You can stop after any module.

!!! tip "Read it before you deploy"
    Modules 00–02 make sense without a deployment. From module 03 on you need a running
    gateway, because you'll call it, read its logs and open its pages.

## Prerequisites

1. **A deployed gateway.** Follow [Agent gateway → Deploy](../agent-gateway.md#deploy)
   ([`deploy/agent-gateway/terraform`](https://github.com/Lariat-Labs/security-tools-sdk/tree/main/deploy/agent-gateway/terraform)). The web chat and Teams bot are optional; modules 07 and 08 need them.
2. **Your account in the gateway's groups**: one tier group, one team group, and at least
   one agent group, all assigned to the gateway app.
3. **Tools**: the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) signed in
   with `az login`, [uv](https://docs.astral.sh/uv/), and this repository cloned. `curl` and
   `python3` come with macOS and most Linux distributions.

## Set your variables (once per terminal)

Every command in this manual uses these variables, so it works on your deployment as written.
Run this from the repository root and adjust the first two lines:

```bash
export RG=rg-agent-gateway        # the resource group you deployed into
export ENTRA_SUFFIX=""            # suffix you gave the app registrations, if any (e.g. "-pilot")

export TENANT_ID=$(az account show --query tenantId -o tsv)
export APIM=$(az resource list -g $RG --resource-type Microsoft.ApiManagement/service --query "[0].name" -o tsv)
export AGW_GATEWAY_URL=https://$APIM.azure-api.net
export AGW_GATEWAY_APP_ID=$(az ad app list --display-name "agent-gateway$ENTRA_SUFFIX" --query "[0].appId" -o tsv)
export CHAT_APP_ID=$(az ad app list --display-name "agent-gateway-chat$ENTRA_SUFFIX" --query "[0].appId" -o tsv)
export TEAMS_APP_ID=$(az ad app list --display-name "agent-gateway-teams$ENTRA_SUFFIX" --query "[0].appId" -o tsv)
export AIS=$(az resource list -g $RG --resource-type Microsoft.CognitiveServices/accounts --query "[0].name" -o tsv)
export EVHNS=$(az resource list -g $RG --resource-type Microsoft.EventHub/namespaces --query "[0].name" -o tsv)
export LAW_ID=$(az monitor log-analytics workspace list -g $RG --query "[0].customerId" -o tsv)
app_url() { echo "https://$(az containerapp list -g $RG --query "[?ends_with(name,'-$1')].properties.configuration.ingress.fqdn | [0]" -o tsv)"; }
export RUNTIME_URL=$(app_url runtime) CHAT_URL=$(app_url chat) BOT_URL=$(app_url teams)

# Print the Azure portal link for any resource in the group: portal <resource-name>
portal() { echo "https://portal.azure.com/#@$TENANT_ID/resource$(az resource list -g $RG --query "[?name=='$1'].id | [0]" -o tsv)/overview"; }
```

Check it worked:

```bash
echo "$APIM $AGW_GATEWAY_APP_ID $AIS $LAW_ID"
portal "$APIM"
```

You should see four non-empty values and a portal URL you can open. Variables last for the
terminal session: re-run the block in a new terminal.

## Modules

| # | Module | You'll learn |
|---|---|---|
| 00 | [The big picture](00-big-picture.md) | The problem, and the five parts that solve it |
| 01 | [The SDK and CLI](01-sdk.md) | Where the security facts come from |
| 02 | [Identity and tokens](02-identity.md) | App registrations, scopes, roles, group claims, JWTs |
| 03 | [The APIM front door](03-front-door.md) | How APIM turns your token into tier, team and entitlement |
| 04 | [Token limits](04-limits.md) | The model route and its six counters |
| 05 | [The agent runtime](05-runtime.md) | The agent loop, budgets, and who it trusts |
| 06 | [Models and quota](06-models.md) | Deployments, regional quota, and why keys are off |
| 07 | [OBO and the web chat](07-obo.md) | On-behalf-of with a federated credential instead of a secret |
| 08 | [The Teams bot](08-teams.md) | Teams SSO, two federated credentials, proactive replies |
| 09 | [Observability](09-observability.md) | Spans, span events, workbooks, following a run |
| 10 | [Cost and chargeback](10-cost.md) | Cost centers, usage events, cost per user |
| 11 | [Infrastructure as code](11-iac.md) | Generators, Terraform, releases with GitHub OIDC |
| 12 | [Copilot Studio (A2A)](12-copilot-studio.md) | Agents as connected agents over A2A, signed in per user |
| 13 | [Wrap-up and teardown](13-wrap-up.md) | The security model, licensing for Teams, cleaning up |
