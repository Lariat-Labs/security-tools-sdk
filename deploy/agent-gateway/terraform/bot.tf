resource "azurerm_bot_service_azure_bot" "this" {
  count                   = local.deploy_teams ? 1 : 0
  name                    = "bot-${var.prefix}-${local.suffix}"
  resource_group_name     = azurerm_resource_group.this.name
  location                = "global"
  sku                     = "F0"
  display_name            = "Agent gateway"
  endpoint                = "https://${azurerm_container_app.teams[0].ingress[0].fqdn}/api/messages"
  microsoft_app_id        = azuread_application_registration.teams[0].client_id
  microsoft_app_type      = "SingleTenant"
  microsoft_app_tenant_id = local.tenant_id
  tags                    = var.tags
}

resource "azurerm_bot_channel_ms_teams" "this" {
  count               = local.deploy_teams ? 1 : 0
  bot_name            = azurerm_bot_service_azure_bot.this[0].name
  resource_group_name = azurerm_resource_group.this.name
  location            = "global"
}

# Teams SSO: "AAD v2 with Federated Credentials" (no client secret). azapi because
# azurerm_bot_connection requires a client secret.
resource "azapi_resource" "teams_sso" {
  count     = local.deploy_teams ? 1 : 0
  type      = "Microsoft.BotService/botServices/connections@2022-09-15"
  name      = "teams-sso"
  parent_id = azurerm_bot_service_azure_bot.this[0].id
  location  = "global"
  body = {
    properties = {
      serviceProviderId = "c00b44ab-5e16-c44c-af26-2fd5bc55eb18" # Aadv2WithFic
      clientId          = azuread_application_registration.teams[0].client_id
      scopes            = "api://botid-${azuread_application_registration.teams[0].client_id}/defaultScopes"
      parameters = [
        { key = "UniqueIdentifier", value = var.teams_sso_unique_id },
        { key = "TokenExchangeUrl", value = "api://botid-${azuread_application_registration.teams[0].client_id}" },
        { key = "TenantId", value = local.tenant_id },
      ]
    }
  }
  depends_on = [azuread_application_federated_identity_credential.teams_token_store]
}
