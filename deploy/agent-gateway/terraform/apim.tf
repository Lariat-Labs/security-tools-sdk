resource "azurerm_api_management" "this" {
  name                = "apim-${var.prefix}-${local.suffix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  publisher_name      = var.publisher_name
  publisher_email     = var.publisher_email
  sku_name            = "StandardV2_1"
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.apim.id]
  }
  tags = var.tags
}

locals {
  apim_rg   = azurerm_resource_group.this.name
  apim_name = azurerm_api_management.this.name
  # Plain API ids for azapi children (azurerm's API ids carry a ";rev=N" suffix).
  models_api_id = "${azurerm_api_management.this.id}/apis/agw-models"
  agents_api_id = "${azurerm_api_management.this.id}/apis/agw-agents"
}

# Environment-specific ids referenced by the rendered policies as {{agw-...}}.
resource "azurerm_api_management_named_value" "ids" {
  for_each = {
    "agw-tenant-id"         = local.tenant_id
    "agw-gateway-app-id"    = local.gateway_app_id
    "agw-apim-client-id"    = azurerm_user_assigned_identity.apim.client_id
    "agw-runtime-client-id" = azurerm_user_assigned_identity.runtime.client_id
  }
  name                = each.key
  display_name        = each.key
  value               = each.value
  api_management_name = local.apim_name
  resource_group_name = local.apim_rg
}

resource "azurerm_api_management_logger" "appinsights" {
  name                = "appinsights"
  api_management_name = local.apim_name
  resource_group_name = local.apim_rg
  resource_id         = azurerm_application_insights.this.id
  application_insights {
    connection_string = azurerm_application_insights.this.connection_string
  }
}

# metrics_enabled turns on custom metrics with dimensions (llm-emit-token-metric).
resource "azurerm_api_management_diagnostic" "appinsights" {
  identifier                = "applicationinsights"
  api_management_name       = local.apim_name
  resource_group_name       = local.apim_rg
  api_management_logger_id  = azurerm_api_management_logger.appinsights.id
  sampling_percentage       = 100
  always_log_errors         = true
  verbosity                 = "information"
  metrics_enabled           = true
  log_client_ip             = false
  http_correlation_protocol = "W3C"
}

resource "azurerm_api_management_logger" "usage" {
  name                = "agw-usage"
  api_management_name = local.apim_name
  resource_group_name = local.apim_rg
  eventhub {
    name                             = azurerm_eventhub.usage.name
    endpoint_uri                     = "${azurerm_eventhub_namespace.this.name}.servicebus.windows.net"
    user_assigned_identity_client_id = azurerm_user_assigned_identity.apim.client_id
  }
  depends_on = [azurerm_role_assignment.apim_sends_usage]
}

# Gateway and LLM logs (token counts per request) to Log Analytics.
resource "azurerm_monitor_diagnostic_setting" "apim" {
  name                           = "to-log-analytics"
  target_resource_id             = azurerm_api_management.this.id
  log_analytics_workspace_id     = azurerm_log_analytics_workspace.this.id
  log_analytics_destination_type = "Dedicated"
  enabled_log {
    category_group = "allLogs"
  }
  enabled_metric {
    category = "AllMetrics"
  }
}

# Token counts per model call in ApiManagementGatewayLlmLog. Prompt and completion
# logging (requests/responses) are separate opt-ins and stay off.
resource "azapi_resource" "llm_logs" {
  type      = "Microsoft.ApiManagement/service/diagnostics@2024-06-01-preview"
  name      = "azuremonitor"
  parent_id = azurerm_api_management.this.id
  body = {
    properties = {
      loggerId           = "/loggers/azuremonitor"
      largeLanguageModel = { logs = "enabled" }
    }
  }
  depends_on = [azurerm_monitor_diagnostic_setting.apim]
}

resource "azurerm_api_management_backend" "models" {
  name                = "agw-models"
  api_management_name = local.apim_name
  resource_group_name = local.apim_rg
  protocol            = "http"
  url                 = "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.openai.azure.com/openai/v1"
  # Fail fast for a while when the deployment keeps throttling.
  circuit_breaker_rule {
    name                       = "model-throttling"
    trip_duration              = "PT1M"
    accept_retry_after_enabled = true
    failure_condition {
      count             = 3
      interval_duration = "PT1M"
      status_code_range {
        min = 429
        max = 429
      }
    }
  }
}

# ----- the model route (the runtime's only way to the models) -------------------------------

resource "azurerm_api_management_api" "models" {
  name                  = "agw-models"
  api_management_name   = local.apim_name
  resource_group_name   = local.apim_rg
  revision              = "1"
  display_name          = "Agent gateway: models"
  path                  = "models/openai/v1"
  protocols             = ["https"]
  subscription_required = false
}

resource "azurerm_api_management_api_operation" "chat_completions" {
  operation_id        = "chat-completions"
  api_name            = azurerm_api_management_api.models.name
  api_management_name = local.apim_name
  resource_group_name = local.apim_rg
  display_name        = "Chat completions"
  method              = "POST"
  url_template        = "/chat/completions"
}

# azapi so the policy is sent as format "xml" (azurerm's api_policy sends "rawxml", and
# render_policies.py emits properly escaped XML).
resource "azapi_resource" "models_policy" {
  type      = "Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview"
  name      = "policy"
  parent_id = local.models_api_id
  body = {
    properties = {
      format = "xml"
      value  = file("${path.module}/${var.models_policy_file}")
    }
  }
  depends_on = [
    azurerm_api_management_api.models,
    azurerm_api_management_named_value.ids,
    azurerm_api_management_backend.models,
    azurerm_api_management_logger.usage,
  ]
}

# ----- the front door users call -----------------------------------------------------------

resource "azurerm_api_management_backend" "runtime" {
  count               = local.deploy_runtime ? 1 : 0
  name                = "agw-runtime"
  api_management_name = local.apim_name
  resource_group_name = local.apim_rg
  protocol            = "http"
  url                 = "https://${azurerm_container_app.runtime[0].ingress[0].fqdn}/agents"
}

resource "azurerm_api_management_api" "agents" {
  count                 = local.deploy_runtime ? 1 : 0
  name                  = "agw-agents"
  api_management_name   = local.apim_name
  resource_group_name   = local.apim_rg
  revision              = "1"
  display_name          = "Agent gateway: agents"
  path                  = "agents"
  protocols             = ["https"]
  subscription_required = false
}

resource "azurerm_api_management_api_operation" "agents" {
  for_each = local.deploy_runtime ? {
    "list-agents" = { name = "List my agents", method = "GET", url = "/" }
    "whoami"      = { name = "My gateway profile (tier, team, cost center, limits)", method = "GET", url = "/me" }
    "run-agent"   = { name = "Run an agent", method = "POST", url = "/{agent}/runs" }
  } : {}
  operation_id        = each.key
  api_name            = azurerm_api_management_api.agents[0].name
  api_management_name = local.apim_name
  resource_group_name = local.apim_rg
  display_name        = each.value.name
  method              = each.value.method
  url_template        = each.value.url

  dynamic "template_parameter" {
    for_each = each.key == "run-agent" ? [1] : []
    content {
      name     = "agent"
      type     = "string"
      required = true
    }
  }
}

resource "azapi_resource" "agents_policy" {
  count     = local.deploy_runtime ? 1 : 0
  type      = "Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview"
  name      = "policy"
  parent_id = local.agents_api_id
  body = {
    properties = {
      format = "xml"
      value  = file("${path.module}/${var.agents_policy_file}")
    }
  }
  depends_on = [
    azurerm_api_management_api.agents,
    azurerm_api_management_named_value.ids,
    azurerm_api_management_backend.runtime,
  ]
}
