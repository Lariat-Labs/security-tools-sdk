# Microsoft Entra: replaces entra.sh. Needs Application Administrator (or Cloud Application
# Administrator) and, for assigning groups to the app, Entra ID P1.
#
# Ids of scopes and roles are fixed UUIDs (uuidv5), so re-applying never changes them.

locals {
  ids = {
    agents_invoke    = uuidv5("url", "https://agent-gateway/${var.prefix}/scope/agents-invoke")
    agents_user      = uuidv5("url", "https://agent-gateway/${var.prefix}/role/agents-user")
    gateway_internal = uuidv5("url", "https://agent-gateway/${var.prefix}/role/gateway-internal")
    access_as_user   = uuidv5("url", "https://agent-gateway/${var.prefix}/scope/access-as-user")
    default_scopes   = uuidv5("url", "https://agent-gateway/${var.prefix}/scope/teams-default")
  }
  azure_cli_client_id = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
  teams_client_ids = {
    desktop_mobile = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"
    web            = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"
  }
  entra_issuer = "https://login.microsoftonline.com/${local.tenant_id}/v2.0"

  # Every tier, team and agent group named in gateway.yaml (assigned to the app when enabled).
  gateway_config = yamldecode(file(var.gateway_config_file))
  config_group_ids = var.assign_config_groups ? toset(concat(
    [for t in local.gateway_config.tiers : t.group_id],
    [for t in try(local.gateway_config.teams, []) : t.group_id],
    [for a in local.gateway_config.agents : a.group_id],
  )) : toset([])
}

# ----- the gateway API (audience of every token APIM accepts) -------------------------------

resource "azuread_application_registration" "gateway" {
  display_name     = var.entra_name_prefix
  sign_in_audience = "AzureADMyOrg"
  # Tokens list only the groups assigned to this app: small tokens, no overage.
  group_membership_claims        = ["ApplicationGroup"]
  requested_access_token_version = 2
}

resource "azuread_application_identifier_uri" "gateway" {
  application_id = azuread_application_registration.gateway.id
  identifier_uri = "api://${azuread_application_registration.gateway.client_id}"
}

resource "azuread_application_permission_scope" "agents_invoke" {
  application_id             = azuread_application_registration.gateway.id
  scope_id                   = local.ids.agents_invoke
  value                      = "Agents.Invoke"
  type                       = "User"
  admin_consent_display_name = "Run agents"
  admin_consent_description  = "Run agents through the agent gateway as the signed-in user."
  user_consent_display_name  = "Run agents"
  user_consent_description   = "Run agents through the agent gateway as you."
}

resource "azuread_application_app_role" "agents_user" {
  application_id       = azuread_application_registration.gateway.id
  role_id              = local.ids.agents_user
  value                = "Agents.User"
  display_name         = "Agent user"
  description          = "Members may use the agent gateway."
  allowed_member_types = ["User"]
}

resource "azuread_application_app_role" "gateway_internal" {
  application_id       = azuread_application_registration.gateway.id
  role_id              = local.ids.gateway_internal
  value                = "Gateway.Internal"
  display_name         = "Gateway internal"
  description          = "APIM and the agent runtime calling each other."
  allowed_member_types = ["Application"]
}

resource "azuread_application_pre_authorized" "gateway_azure_cli" {
  count                = var.preauthorize_azure_cli ? 1 : 0
  application_id       = azuread_application_registration.gateway.id
  authorized_client_id = local.azure_cli_client_id
  permission_ids       = [azuread_application_permission_scope.agents_invoke.scope_id]
}

resource "azuread_service_principal" "gateway" {
  client_id = azuread_application_registration.gateway.client_id
  # Only assigned users and groups can get a token at all.
  app_role_assignment_required = true
}

# APIM and the runtime call each other as Gateway.Internal.
resource "azuread_app_role_assignment" "internal" {
  for_each = {
    apim    = azurerm_user_assigned_identity.apim.principal_id
    runtime = azurerm_user_assigned_identity.runtime.principal_id
  }
  app_role_id         = azuread_application_app_role.gateway_internal.role_id
  principal_object_id = each.value
  resource_object_id  = azuread_service_principal.gateway.object_id
}

resource "azuread_app_role_assignment" "config_groups" {
  for_each            = local.config_group_ids
  app_role_id         = azuread_application_app_role.agents_user.role_id
  principal_object_id = each.value
  resource_object_id  = azuread_service_principal.gateway.object_id
}

# ----- web chat app (SPA + API; its backend does OBO with a federated credential) ------------

resource "azuread_application_registration" "chat" {
  count                          = local.deploy_chat ? 1 : 0
  display_name                   = "${var.entra_name_prefix}-chat"
  sign_in_audience               = "AzureADMyOrg"
  requested_access_token_version = 2
}

resource "azuread_application_identifier_uri" "chat" {
  count          = local.deploy_chat ? 1 : 0
  application_id = azuread_application_registration.chat[0].id
  identifier_uri = "api://${azuread_application_registration.chat[0].client_id}"
}

resource "azuread_application_permission_scope" "access_as_user" {
  count                      = local.deploy_chat ? 1 : 0
  application_id             = azuread_application_registration.chat[0].id
  scope_id                   = local.ids.access_as_user
  value                      = "access_as_user"
  type                       = "User"
  admin_consent_display_name = "Use the agent chat"
  admin_consent_description  = "Use the agent gateway chat as the signed-in user."
  user_consent_display_name  = "Use the agent chat"
  user_consent_description   = "Use the agent gateway chat as you."
}

resource "azuread_application_api_access" "chat_gateway" {
  count          = local.deploy_chat ? 1 : 0
  application_id = azuread_application_registration.chat[0].id
  api_client_id  = local.gateway_app_id
  scope_ids      = [azuread_application_permission_scope.agents_invoke.scope_id]
}

resource "azuread_application_api_access" "chat_self" {
  count          = local.deploy_chat ? 1 : 0
  application_id = azuread_application_registration.chat[0].id
  api_client_id  = azuread_application_registration.chat[0].client_id
  scope_ids      = [azuread_application_permission_scope.access_as_user[0].scope_id]
}

resource "azuread_application_pre_authorized" "chat_azure_cli" {
  count                = local.deploy_chat && var.preauthorize_azure_cli ? 1 : 0
  application_id       = azuread_application_registration.chat[0].id
  authorized_client_id = local.azure_cli_client_id
  permission_ids       = [azuread_application_permission_scope.access_as_user[0].scope_id]
}

resource "azuread_application_redirect_uris" "chat_spa" {
  count          = local.deploy_chat ? 1 : 0
  application_id = azuread_application_registration.chat[0].id
  type           = "SPA"
  redirect_uris = [
    "https://${azurerm_container_app.chat[0].ingress[0].fqdn}/",
    "http://localhost:8000/",
  ]
}

resource "azuread_service_principal" "chat" {
  count     = local.deploy_chat ? 1 : 0
  client_id = azuread_application_registration.chat[0].client_id
}

# Tenant-wide consent (OBO can't show a consent prompt).
resource "azuread_service_principal_delegated_permission_grant" "chat" {
  for_each = local.deploy_chat ? {
    gateway = { resource = azuread_service_principal.gateway.object_id, claim = "Agents.Invoke" }
    self    = { resource = azuread_service_principal.chat[0].object_id, claim = "access_as_user" }
  } : {}
  service_principal_object_id          = azuread_service_principal.chat[0].object_id
  resource_service_principal_object_id = each.value.resource
  claim_values                         = [each.value.claim]
}

resource "azuread_application_federated_identity_credential" "chat_mi" {
  count          = local.deploy_chat ? 1 : 0
  application_id = azuread_application_registration.chat[0].id
  display_name   = "agw-chat-managed-identity"
  description    = "The chat backend's managed identity is this app's client credential (no secret)."
  issuer         = local.entra_issuer
  subject        = azurerm_user_assigned_identity.chat.principal_id
  audiences      = ["api://AzureADTokenExchange"]
}

# ----- Teams bot app (bot identity + Teams SSO app, api://botid-<app-id>) ----------------------

resource "azuread_application_registration" "teams" {
  count                          = local.deploy_teams ? 1 : 0
  display_name                   = "${var.entra_name_prefix}-teams"
  sign_in_audience               = "AzureADMyOrg"
  requested_access_token_version = 2
  lifecycle {
    precondition {
      condition     = var.tenant_id_base64url != ""
      error_message = "Set tenant_id_base64url to deploy the Teams bot (see its description)."
    }
  }
}

resource "azuread_application_identifier_uri" "teams" {
  count          = local.deploy_teams ? 1 : 0
  application_id = azuread_application_registration.teams[0].id
  identifier_uri = "api://botid-${azuread_application_registration.teams[0].client_id}"
}

resource "azuread_application_permission_scope" "default_scopes" {
  count                      = local.deploy_teams ? 1 : 0
  application_id             = azuread_application_registration.teams[0].id
  scope_id                   = local.ids.default_scopes
  value                      = "defaultScopes"
  type                       = "User"
  admin_consent_display_name = "Use the agent gateway bot"
  admin_consent_description  = "Use the agent gateway bot as the signed-in user."
  user_consent_display_name  = "Use the agent gateway bot"
  user_consent_description   = "Use the agent gateway bot as you."
}

# Teams desktop/mobile and web get SSO tokens without a consent prompt.
resource "azuread_application_pre_authorized" "teams_clients" {
  for_each             = local.deploy_teams ? local.teams_client_ids : {}
  application_id       = azuread_application_registration.teams[0].id
  authorized_client_id = each.value
  permission_ids       = [azuread_application_permission_scope.default_scopes[0].scope_id]
}

resource "azuread_application_redirect_uris" "teams_web" {
  count          = local.deploy_teams ? 1 : 0
  application_id = azuread_application_registration.teams[0].id
  type           = "Web"
  redirect_uris  = ["https://token.botframework.com/.auth/web/redirect"]
}

resource "azuread_application_api_access" "teams_gateway" {
  count          = local.deploy_teams ? 1 : 0
  application_id = azuread_application_registration.teams[0].id
  api_client_id  = local.gateway_app_id
  scope_ids      = [azuread_application_permission_scope.agents_invoke.scope_id]
}

resource "azuread_application_api_access" "teams_self" {
  count          = local.deploy_teams ? 1 : 0
  application_id = azuread_application_registration.teams[0].id
  api_client_id  = azuread_application_registration.teams[0].client_id
  scope_ids      = [azuread_application_permission_scope.default_scopes[0].scope_id]
}

resource "azuread_service_principal" "teams" {
  count     = local.deploy_teams ? 1 : 0
  client_id = azuread_application_registration.teams[0].client_id
}

resource "azuread_service_principal_delegated_permission_grant" "teams" {
  for_each = local.deploy_teams ? {
    gateway = { resource = azuread_service_principal.gateway.object_id, claim = "Agents.Invoke" }
    self    = { resource = azuread_service_principal.teams[0].object_id, claim = "defaultScopes" }
  } : {}
  service_principal_object_id          = azuread_service_principal.teams[0].object_id
  resource_service_principal_object_id = each.value.resource
  claim_values                         = [each.value.claim]
}

# (1) Azure Bot token store -> the OAuth connection ("AAD v2 with Federated Credentials").
resource "azuread_application_federated_identity_credential" "teams_token_store" {
  count          = local.deploy_teams ? 1 : 0
  application_id = azuread_application_registration.teams[0].id
  display_name   = "bot-token-store"
  description    = "Azure Bot OAuth connection (AAD v2 with Federated Credentials)"
  issuer         = local.entra_issuer
  # 9ExAW52n_ky4ZiS_jhpJIQ = base64url id of the Bot Service Token Store first-party app.
  subject   = "/eid1/c/pub/t/${var.tenant_id_base64url}/a/9ExAW52n_ky4ZiS_jhpJIQ/${var.teams_sso_unique_id}"
  audiences = ["api://AzureADTokenExchange"]
}

# (2) The bot's managed identity -> Bot Service auth and OBO (AuthType=FederatedCredentials).
resource "azuread_application_federated_identity_credential" "teams_bot_mi" {
  count          = local.deploy_teams ? 1 : 0
  application_id = azuread_application_registration.teams[0].id
  display_name   = "bot-managed-identity"
  description    = "The bot's managed identity is this app's credential"
  issuer         = local.entra_issuer
  subject        = azurerm_user_assigned_identity.bot.principal_id
  audiences      = ["api://AzureADTokenExchange"]
}
