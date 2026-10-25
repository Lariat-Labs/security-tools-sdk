resource "azurerm_cognitive_account" "foundry" {
  name                          = "ais-${var.prefix}-${local.suffix}"
  resource_group_name           = azurerm_resource_group.this.name
  location                      = var.location
  kind                          = "AIServices"
  sku_name                      = "S0"
  custom_subdomain_name         = "ais-${var.prefix}-${local.suffix}"
  local_auth_enabled            = false # Entra only: APIM's identity is the sole caller
  public_network_access_enabled = true
  project_management_enabled    = true # Foundry project for the trace view
  identity {
    type = "SystemAssigned"
  }
  tags = var.tags
}

# The account stays busy for a while after its project and deployments are deleted, and
# rejects its own delete until then. Destroy waits here between the children and the account.
resource "time_sleep" "foundry_settle" {
  destroy_duration = "90s"
  depends_on       = [azurerm_cognitive_account.foundry]
}

resource "azurerm_cognitive_deployment" "models" {
  for_each             = { for d in var.model_deployments : d.name => d }
  name                 = each.value.name
  cognitive_account_id = azurerm_cognitive_account.foundry.id
  model {
    format  = "OpenAI"
    name    = each.value.model
    version = each.value.version
  }
  sku {
    name     = each.value.sku
    capacity = each.value.capacity
  }
  depends_on = [time_sleep.foundry_settle]
}

resource "azurerm_role_assignment" "apim_calls_models" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = local.role_openai_user
  principal_id         = azurerm_user_assigned_identity.apim.principal_id
  principal_type       = "ServicePrincipal"
}

# Foundry project whose agent trace view shows the runtime's traces (register_agents.py
# registers the agents as external agents; Foundry matches spans by gen_ai.agent.id).
resource "azurerm_cognitive_account_project" "agents" {
  name                 = "agents"
  cognitive_account_id = azurerm_cognitive_account.foundry.id
  location             = var.location
  display_name         = "Agent gateway"
  description          = "Observability and evaluation for the agent gateway runtime."
  identity {
    type = "SystemAssigned"
  }
  # The account rejects concurrent updates.
  depends_on = [azurerm_cognitive_deployment.models]
}

# Foundry's agent Monitor tab queries Application Insights as the project's managed identity.
resource "azurerm_role_assignment" "project_reads_telemetry" {
  scope                = azurerm_application_insights.this.id
  role_definition_name = "Monitoring Reader"
  principal_id         = azurerm_cognitive_account_project.agents.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azapi_resource" "foundry_appinsights" {
  type      = "Microsoft.CognitiveServices/accounts/connections@2025-06-01"
  name      = "appinsights"
  parent_id = azurerm_cognitive_account.foundry.id
  body = {
    properties = {
      category      = "AppInsights"
      target        = azurerm_application_insights.this.id
      authType      = "ApiKey"
      isSharedToAll = true
      credentials   = { key = azurerm_application_insights.this.connection_string }
      metadata      = { ApiType = "Azure", ResourceId = azurerm_application_insights.this.id }
    }
  }
  depends_on = [azurerm_cognitive_account_project.agents]
}

# Foundry's portal runs Insights scans and evaluations with gpt-5-mini as the signed-in person,
# which needs model inference on the account. Foundry User (formerly Azure AI User) grants it
# by role id, since the display name changed.
resource "azurerm_role_assignment" "foundry_users" {
  for_each           = toset(concat([data.azurerm_client_config.current.object_id], var.foundry_users))
  scope              = azurerm_cognitive_account.foundry.id
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/53ca6127-db72-4b80-b1b0-d745d6d5456d"
  principal_id       = each.value
}
