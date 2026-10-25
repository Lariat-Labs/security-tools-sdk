# Agent gateway: Terraform

The Terraform equivalent of `../infra/main.bicep` **and** `../entra.sh`: one root module
creates the Entra app registrations, identities, APIM, models, monitoring and (once
images exist) the runtime, web chat and Teams bot.

| File | What |
|---|---|
| `entra.tf` | Gateway, chat and Teams app registrations, scopes, roles, grants, federated credentials (azuread) |
| `identity.tf` | User-assigned managed identities |
| `models.tf` | AI Services (Foundry) account, model deployments, Foundry project + App Insights connection |
| `apim.tf` | APIM Standard v2, named values, loggers, diagnostics, backends, both APIs and their policies |
| `apps.tf` | Container registry, Container Apps environment, runtime / chat / Teams containers, runtime auth |
| `bot.tf` | Azure Bot, Teams channel, federated Teams SSO connection |
| `monitoring.tf` | Log Analytics (+30-day AppTraces), App Insights, Event Hubs, workbooks |

Providers: `azurerm` for most resources, `azuread` for Entra, `azapi` where azurerm can't do
exactly what's needed (policies in `xml` format, since azurerm sends `rawxml`; Container
Apps auth; APIM LLM logging; the Foundry App Insights connection; the federated Bot OAuth
connection, since azurerm's requires a client secret).

```bash
cd deploy/agent-gateway/terraform
cp terraform.tfvars.example terraform.tfvars        # fill in
terraform init
terraform apply                                      # 1st: everything except the containers

# build and push images to the new registry, set runtime_image (+ chat_image, teams_image)
terraform apply                                      # 2nd: containers, front door, chat, bot
```

Needs: Owner (or Contributor + User Access Administrator) on the subscription, and
Application Administrator in Entra. State contains secrets (connection strings): use a
remote backend with encryption and restricted access (for example an Azure Storage
backend with Entra auth) rather than local state.

## Tear down

```bash
terraform destroy
```

This removes everything above, Entra included. The provider `features` in `main.tf` purge the
soft-deleted APIM and AI Services account and permanently delete the Log Analytics workspace,
because resource names are deterministic and a soft-deleted one would block the next apply.
Deleted app registrations stay in Entra's *Deleted applications* for 30 days. They can't
collide with a new deployment, since their identifier URIs use their own app IDs.

The pilot environment was deployed with Bicep; Terraform is validated with `terraform
validate` and a full `terraform plan` (84 resources) but has not been applied.
