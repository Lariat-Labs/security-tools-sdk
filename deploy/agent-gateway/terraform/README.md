# Agent gateway: Terraform

One root module creates the Entra app registrations, identities, APIM, models, monitoring and (once
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
| `github.tf` | Optional identity the release workflow signs in as (OIDC), with push and roll-out rights only |

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

## Releases from GitHub

`.github/workflows/agent-gateway-release.yml` builds the containers, pushes them to the
registry and rolls out a new revision with `azure/container-apps-deploy-action`, on a tag
such as `agw-v0.7.0`. It signs in with OIDC, so GitHub holds no Azure secret:

1. Set `github_repository = "<owner>/<name>"` and apply. This creates an identity that trusts
   only that repository's `agent-gateway` environment, with `AcrPush` (+ Reader) on the
   registry and Container Apps Contributor on the apps.
   If the repository uses GitHub's immutable subject claims, also set
   `github_oidc_subject_prefix` (see the variable's description for the one-liner).
2. Create the `agent-gateway` environment in the repository and add the variables from
   `terraform output github_variables`. None of them is secret.
3. Push a tag: `git tag agw-v0.7.0 && git push origin agw-v0.7.0`.

After the first revision the workflow owns the images: Terraform ignores image changes, so
changing `runtime_image` later has no effect.
