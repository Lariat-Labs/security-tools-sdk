# Agent gateway on Azure: Terraform equivalent of infra/main.bicep + entra.sh.
#
# One `terraform apply` creates the Entra app registrations, identities, APIM, models,
# monitoring and (once images exist) the runtime, web chat and Teams bot. See README.md.
#
# Providers: azurerm for most resources, azuread for Entra, azapi where azurerm can't
# express exactly what the Bicep does (policies in XML format, Container Apps auth,
# APIM LLM logging, the Foundry App Insights connection, the federated Bot OAuth connection).

provider "azurerm" {
  # Make `terraform destroy` complete and repeatable: names are deterministic, so anything left
  # soft-deleted would block the next apply.
  features {
    api_management {
      purge_soft_delete_on_destroy = true
    }
    cognitive_account {
      purge_soft_delete_on_destroy = true
    }
    log_analytics_workspace {
      permanently_delete_on_destroy = true
    }
    resource_group {
      # The group is created by this configuration and holds only the gateway, so destroy
      # also removes anything Azure added to it on its own (such as App Insights' "Failure
      # Anomalies" alert rule). application_insights.disable_generated_rule is deliberately
      # not used: it waits for that rule, which never appears on subscriptions without the
      # Microsoft.AlertsManagement provider registered, so the apply hangs.
      prevent_deletion_if_contains_resources = false
    }
  }
  subscription_id = var.subscription_id
}

provider "azapi" {}

provider "azuread" {}

data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "this" {
  name     = var.resource_group_name
  location = var.location
  tags     = var.tags
}

locals {
  tenant_id = data.azurerm_client_config.current.tenant_id
  # Stable per subscription + resource group, like Bicep's uniqueString(resourceGroup().id).
  suffix = substr(sha1("${var.subscription_id}/${var.resource_group_name}"), 0, 13)

  deploy_runtime = var.runtime_image != ""
  deploy_chat    = local.deploy_runtime && var.chat_image != ""
  deploy_teams   = local.deploy_runtime && var.teams_image != ""

  gateway_app_id   = azuread_application_registration.gateway.client_id
  gateway_audience = "api://${azuread_application_registration.gateway.client_id}"

  # Built-in role definition names
  role_openai_user     = "Cognitive Services OpenAI User"
  role_acr_pull        = "AcrPull"
  role_eventhub_sender = "Azure Event Hubs Data Sender"

  # Every span is kept: the default rate-limited sampler (5 traces/s) drops spans mid-run.
  otel_always_on = { name = "OTEL_TRACES_SAMPLER", value = "always_on" }
}
