# Release workflow (.github/workflows/agent-gateway-release.yml): GitHub Actions signs in with
# OIDC as this identity. No secret: Entra trusts only tokens GitHub issues for the named
# repository's environment. It can push images and update the gateway's container apps,
# nothing else.

locals {
  github_deploy = var.github_repository != ""
  # Repositories using GitHub's immutable subject claims put ids in it (owner@id/repo@id).
  github_subject_prefix = coalesce(var.github_oidc_subject_prefix, "repo:${var.github_repository}")
  container_apps = merge(
    local.deploy_runtime ? { runtime = azurerm_container_app.runtime[0].id } : {},
    local.deploy_chat ? { chat = azurerm_container_app.chat[0].id } : {},
    local.deploy_teams ? { teams = azurerm_container_app.teams[0].id } : {},
  )
}

resource "azurerm_user_assigned_identity" "github" {
  count               = local.github_deploy ? 1 : 0
  name                = "id-${var.prefix}-github"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "github" {
  count                     = local.github_deploy ? 1 : 0
  name                      = "github-${var.github_environment}"
  user_assigned_identity_id = azurerm_user_assigned_identity.github[0].id
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "${local.github_subject_prefix}:environment:${var.github_environment}"
  audience                  = ["api://AzureADTokenExchange"]
}

# az acr login looks the registry up (Reader) before getting a push token (AcrPush).
resource "azurerm_role_assignment" "github_registry" {
  for_each             = local.github_deploy ? toset(["AcrPush", "Reader"]) : toset([])
  scope                = azurerm_container_registry.this.id
  role_definition_name = each.value
  principal_id         = azurerm_user_assigned_identity.github[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "github_apps" {
  for_each             = local.github_deploy ? local.container_apps : {}
  scope                = each.value
  role_definition_name = "Container Apps Contributor"
  principal_id         = azurerm_user_assigned_identity.github[0].principal_id
  principal_type       = "ServicePrincipal"
}
