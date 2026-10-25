output "gateway_url" {
  value = azurerm_api_management.this.gateway_url
}

output "agents_endpoint" {
  value = "${azurerm_api_management.this.gateway_url}/agents"
}

output "gateway_app_id" {
  description = "Audience api://<this>; AGW_GATEWAY_APP_ID for client.py."
  value       = local.gateway_app_id
}

output "registry_login_server" {
  value = azurerm_container_registry.this.login_server
}

output "registry_name" {
  value = azurerm_container_registry.this.name
}

output "chat_url" {
  value = local.deploy_chat ? "https://${azurerm_container_app.chat[0].ingress[0].fqdn}" : ""
}

output "chat_app_id" {
  value = local.deploy_chat ? azuread_application_registration.chat[0].client_id : ""
}

output "teams_app_id" {
  description = "Use with teams/package.py --app-id."
  value       = local.deploy_teams ? azuread_application_registration.teams[0].client_id : ""
}

output "teams_bot_host" {
  description = "Use with teams/package.py --bot-host."
  value       = local.deploy_teams ? azurerm_container_app.teams[0].ingress[0].fqdn : ""
}

output "foundry_project_endpoint" {
  description = "Use with register_agents.py --endpoint."
  value       = "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.services.ai.azure.com/api/projects/${azurerm_cognitive_account_project.agents.name}"
}

output "log_analytics_workspace_id" {
  description = "Customer id, for infra/workbooks/validate.py."
  value       = azurerm_log_analytics_workspace.this.workspace_id
}

output "usage_event_hub" {
  value = "${azurerm_eventhub_namespace.this.name}/${azurerm_eventhub.usage.name}"
}

output "copilot_studio_client_id" {
  description = "Client ID for Copilot Studio's A2A OAuth 2.0 connection (create its secret in the portal)."
  value       = var.enable_copilot_studio ? azuread_application_registration.copilot_studio[0].client_id : ""
}

output "a2a_endpoints" {
  description = "A2A endpoint per agent, for Copilot Studio (Add agent > A2A agent)."
  value       = local.deploy_runtime ? "${azurerm_api_management.this.gateway_url}/agents/<agent>/a2a" : ""
}
