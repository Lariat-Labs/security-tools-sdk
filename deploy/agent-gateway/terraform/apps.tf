resource "azurerm_container_registry" "this" {
  name                = "cr${var.prefix}${local.suffix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = var.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  for_each = {
    runtime = azurerm_user_assigned_identity.runtime.principal_id
    chat    = azurerm_user_assigned_identity.chat.principal_id
    bot     = azurerm_user_assigned_identity.bot.principal_id
  }
  scope                = azurerm_container_registry.this.id
  role_definition_name = local.role_acr_pull
  principal_id         = each.value
  principal_type       = "ServicePrincipal"
}

resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${var.prefix}-${local.suffix}"
  resource_group_name        = azurerm_resource_group.this.name
  location                   = var.location
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  tags                       = var.tags
}

# ----- agent runtime ---------------------------------------------------------------------------

resource "azurerm_container_app" "runtime" {
  count                        = local.deploy_runtime ? 1 : 0
  name                         = "ca-${var.prefix}-runtime"
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime.id]
  }
  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.runtime.id
  }
  ingress {
    external_enabled = true
    target_port      = 8000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 1
    max_replicas = 5
    container {
      name   = "runtime"
      image  = var.runtime_image
      cpu    = 0.5
      memory = "1Gi"

      dynamic "env" {
        for_each = {
          AGW_MODEL_BASE_URL                    = "${azurerm_api_management.this.gateway_url}/models/openai/v1"
          AGW_GATEWAY_SCOPE                     = "${local.gateway_audience}/.default"
          AGW_PUBLIC_BASE_URL                   = azurerm_api_management.this.gateway_url
          AZURE_CLIENT_ID                       = azurerm_user_assigned_identity.runtime.client_id
          APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.this.connection_string
          OTEL_SERVICE_NAME                     = "agent-gateway-runtime"
          OTEL_TRACES_SAMPLER                   = local.otel_always_on.value
          AGW_CAPTURE_CONTENT                   = var.capture_content ? "true" : "false"
          AGW_OBO_CLIENT_IDS = join(",", compact([
            local.deploy_chat ? azuread_application_registration.chat[0].client_id : "",
            local.deploy_teams ? azuread_application_registration.teams[0].client_id : "",
          ]))
          SECSDK_SEC_USER_AGENT = var.sec_user_agent
          SECSDK_TIMEOUT        = "30"
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      liveness_probe {
        transport = "HTTP"
        path      = "/healthz"
        port      = 8000
      }
    }
  }
  tags       = var.tags
  depends_on = [azurerm_role_assignment.acr_pull]
  lifecycle {
    # The image variable sets the first revision; after that the release workflow rolls out
    # images (see github.tf), and Terraform leaves them alone.
    ignore_changes = [template[0].container[0].image]
  }
}

# Only the gateway's identity may call the runtime, so its x-agw-* headers can be trusted.
# (azurerm has no Container Apps auth resource.)
resource "azapi_resource" "runtime_auth" {
  count     = local.deploy_runtime ? 1 : 0
  type      = "Microsoft.App/containerApps/authConfigs@2024-03-01"
  name      = "current"
  parent_id = azurerm_container_app.runtime[0].id
  body = {
    properties = {
      platform         = { enabled = true }
      globalValidation = { unauthenticatedClientAction = "Return401" }
      identityProviders = {
        azureActiveDirectory = {
          enabled = true
          registration = {
            clientId     = local.gateway_app_id
            openIdIssuer = local.entra_issuer
          }
          validation = {
            allowedAudiences = [local.gateway_audience, local.gateway_app_id]
            defaultAuthorizationPolicy = {
              allowedApplications = [azurerm_user_assigned_identity.apim.client_id]
            }
          }
        }
      }
    }
  }
}

# ----- web chat (OBO) --------------------------------------------------------------------------

resource "azurerm_container_app" "chat" {
  count                        = local.deploy_chat ? 1 : 0
  name                         = "ca-${var.prefix}-chat"
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.chat.id]
  }
  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.chat.id
  }
  ingress {
    external_enabled = true
    target_port      = 8000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 0
    max_replicas = 2
    container {
      name   = "chat"
      image  = var.chat_image
      cpu    = 0.25
      memory = "0.5Gi"

      dynamic "env" {
        for_each = {
          AGW_TENANT_ID                         = local.tenant_id
          AGW_CHAT_APP_ID                       = azuread_application_registration.chat[0].client_id
          AGW_GATEWAY_URL                       = azurerm_api_management.this.gateway_url
          AGW_GATEWAY_APP_ID                    = local.gateway_app_id
          AZURE_CLIENT_ID                       = azurerm_user_assigned_identity.chat.client_id
          APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.this.connection_string
          OTEL_SERVICE_NAME                     = "agent-gateway-chat"
          OTEL_TRACES_SAMPLER                   = local.otel_always_on.value
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      liveness_probe {
        transport = "HTTP"
        path      = "/healthz"
        port      = 8000
      }
    }
  }
  tags       = var.tags
  depends_on = [azurerm_role_assignment.acr_pull]
  lifecycle {
    # The image variable sets the first revision; after that the release workflow rolls out
    # images (see github.tf), and Terraform leaves them alone.
    ignore_changes = [template[0].container[0].image]
  }
}

# ----- Teams bot -------------------------------------------------------------------------------

resource "azurerm_container_app" "teams" {
  count                        = local.deploy_teams ? 1 : 0
  name                         = "ca-${var.prefix}-teams"
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.bot.id]
  }
  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.bot.id
  }
  ingress {
    external_enabled = true
    target_port      = 3978
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    # Conversation state is in memory: exactly one replica.
    min_replicas = 1
    max_replicas = 1
    container {
      name   = "bot"
      image  = var.teams_image
      cpu    = 0.25
      memory = "0.5Gi"

      dynamic "env" {
        for_each = {
          CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE                                           = "FederatedCredentials"
          CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID                                           = azuread_application_registration.teams[0].client_id
          CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID                                           = local.tenant_id
          CONNECTIONS__SERVICE_CONNECTION__SETTINGS__FEDERATEDCLIENTID                                  = azurerm_user_assigned_identity.bot.client_id
          AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GATEWAY__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME = "teams-sso"
          AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GATEWAY__SETTINGS__OBOCONNECTIONNAME           = "SERVICE_CONNECTION"
          AGW_GATEWAY_URL                                                                               = azurerm_api_management.this.gateway_url
          AGW_GATEWAY_APP_ID                                                                            = local.gateway_app_id
          APPLICATIONINSIGHTS_CONNECTION_STRING                                                         = azurerm_application_insights.this.connection_string
          OTEL_SERVICE_NAME                                                                             = "agent-gateway-teams"
          OTEL_TRACES_SAMPLER                                                                           = local.otel_always_on.value
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      liveness_probe {
        transport = "HTTP"
        path      = "/healthz"
        port      = 3978
      }
    }
  }
  tags       = var.tags
  depends_on = [azurerm_role_assignment.acr_pull]
  lifecycle {
    # The image variable sets the first revision; after that the release workflow rolls out
    # images (see github.tf), and Terraform leaves them alone.
    ignore_changes = [template[0].container[0].image]
  }
}
