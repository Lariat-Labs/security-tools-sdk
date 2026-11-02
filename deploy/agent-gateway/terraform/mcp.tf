# Optional proof of concept: one secsdk operation (kev.lookup) as an MCP tool in APIM.
#
#   MCP client --Entra token--> MCP server "agw-mcp" (/secsdk-mcp/mcp, tool kev_lookup)
#     --> REST API "agw-tools" (GET /tools/kev-lookup?cve_id=)  [APIM's "REST API as MCP" mode]
#     --> runtime container GET /tools/kev-lookup --> secsdk kev.lookup
#
# Callers use the same Entra app and scope as the agents API (Agents.Invoke), so only users
# assigned to the gateway app get a token. Calls are rate limited per user (oid). The runtime
# accepts only APIM's identity, as for agent runs. Off unless enable_mcp_tools = true.
#
# azurerm has no MCP server resources yet, hence azapi with the 2025-09-01-preview API.

locals {
  deploy_mcp   = local.deploy_runtime && var.enable_mcp_tools
  tools_api_id = "${azurerm_api_management.this.id}/apis/agw-tools"
  mcp_api_id   = "${azurerm_api_management.this.id}/apis/agw-mcp"
  mcp_path     = "secsdk-mcp"

  # Same token check as the agents API front door.
  mcp_validate_token = <<-XML
    <validate-azure-ad-token tenant-id="{{agw-tenant-id}}" output-token-variable-name="agw-jwt"
        failed-validation-httpcode="401"
        failed-validation-error-message="Sign in with a Microsoft Entra token for the agent gateway.">
      <audiences>
        <audience>api://{{agw-gateway-app-id}}</audience>
        <audience>{{agw-gateway-app-id}}</audience>
      </audiences>
      <required-claims>
        <claim name="scp" match="any" separator=" ">
          <value>Agents.Invoke</value>
        </claim>
      </required-claims>
    </validate-azure-ad-token>
  XML
}

# ----- the REST operation behind the tool ------------------------------------------------------

resource "azurerm_api_management_backend" "runtime_tools" {
  count               = local.deploy_mcp ? 1 : 0
  name                = "agw-runtime-tools"
  api_management_name = local.apim_name
  resource_group_name = local.apim_rg
  protocol            = "http"
  url                 = "https://${azurerm_container_app.runtime[0].ingress[0].fqdn}/tools"
}

resource "azurerm_api_management_api" "tools" {
  count                 = local.deploy_mcp ? 1 : 0
  name                  = "agw-tools"
  api_management_name   = local.apim_name
  resource_group_name   = local.apim_rg
  revision              = "1"
  display_name          = "Agent gateway: tools (REST, behind the MCP server)"
  path                  = "tools"
  protocols             = ["https"]
  subscription_required = false
}

# The tool's name, description and input schema come from this operation.
resource "azurerm_api_management_api_operation" "kev_lookup" {
  count               = local.deploy_mcp ? 1 : 0
  operation_id        = "kev-lookup"
  api_name            = azurerm_api_management_api.tools[0].name
  api_management_name = local.apim_name
  resource_group_name = local.apim_rg
  display_name        = "Look up a CVE in CISA KEV"
  description         = "Return the CISA Known Exploited Vulnerabilities (KEV) entry for one CVE: vendor, product, date added, due date, ransomware use. Returns ok=false with error.code not_found when the CVE is not in KEV."
  method              = "GET"
  url_template        = "/kev-lookup"

  request {
    query_parameter {
      name        = "cve_id"
      type        = "string"
      required    = true
      description = "CVE id, e.g. CVE-2024-3400"
    }
  }
}

# Direct calls to /tools need the same token as MCP; the runtime gets the user id, not the token.
resource "azurerm_api_management_api_policy" "tools" {
  count               = local.deploy_mcp ? 1 : 0
  api_name            = azurerm_api_management_api.tools[0].name
  api_management_name = local.apim_name
  resource_group_name = local.apim_rg
  xml_content         = <<-XML
    <policies>
      <inbound>
        <base />
        ${indent(4, local.mcp_validate_token)}
        <set-header name="x-agw-user" exists-action="override">
          <value>@(((Jwt)context.Variables["agw-jwt"]).Claims.GetValueOrDefault("oid", ""))</value>
        </set-header>
        <set-header name="x-agw-client" exists-action="override">
          <value>@{ var c = ((Jwt)context.Variables["agw-jwt"]).Claims; var azp = c.GetValueOrDefault("azp", ""); return azp != "" ? azp : c.GetValueOrDefault("appid", ""); }</value>
        </set-header>
        <set-backend-service backend-id="agw-runtime-tools" />
        <authentication-managed-identity resource="api://{{agw-gateway-app-id}}" client-id="{{agw-apim-client-id}}" />
      </inbound>
      <backend>
        <base />
      </backend>
      <outbound>
        <base />
      </outbound>
      <on-error>
        <base />
      </on-error>
    </policies>
  XML
  depends_on = [
    azurerm_api_management_api_operation.kev_lookup,
    azurerm_api_management_backend.runtime_tools,
    azurerm_api_management_named_value.ids,
  ]
}

# ----- the MCP server --------------------------------------------------------------------------

resource "azapi_resource" "mcp_server" {
  count                     = local.deploy_mcp ? 1 : 0
  type                      = "Microsoft.ApiManagement/service/apis@2025-09-01-preview"
  name                      = "agw-mcp"
  parent_id                 = azurerm_api_management.this.id
  schema_validation_enabled = false # the provider's embedded schemas predate MCP servers
  body = {
    properties = {
      type                 = "mcp"
      displayName          = "Agent gateway: secsdk MCP tools"
      description          = "secsdk operations as MCP tools, behind the agent gateway's Entra sign-in."
      path                 = local.mcp_path
      protocols            = ["https"]
      subscriptionRequired = false
    }
  }
  depends_on = [azurerm_api_management_api_policy.tools]
}

resource "azapi_resource" "mcp_tool_kev_lookup" {
  count                     = local.deploy_mcp ? 1 : 0
  type                      = "Microsoft.ApiManagement/service/apis/tools@2025-09-01-preview"
  name                      = "kev_lookup"
  parent_id                 = azapi_resource.mcp_server[0].id
  schema_validation_enabled = false
  body = {
    properties = {
      displayName = "kev_lookup"
      description = azurerm_api_management_api_operation.kev_lookup[0].description
      operationId = "${local.tools_api_id}/operations/kev-lookup"
    }
  }
}

resource "azapi_resource" "mcp_policy" {
  count                     = local.deploy_mcp ? 1 : 0
  type                      = "Microsoft.ApiManagement/service/apis/policies@2025-09-01-preview"
  name                      = "policy"
  parent_id                 = azapi_resource.mcp_server[0].id
  schema_validation_enabled = false
  body = {
    properties = {
      format = "rawxml"
      # Don't read context.Response.Body here: buffering breaks MCP streaming.
      value = <<-XML
        <policies>
          <inbound>
            <base />
            ${indent(6, local.mcp_validate_token)}
            <rate-limit-by-key calls="${var.mcp_calls_per_minute}" renewal-period="60"
                counter-key='@("mcp:" + ((Jwt)context.Variables["agw-jwt"]).Claims.GetValueOrDefault("oid", ""))' />
          </inbound>
          <backend>
            <base />
          </backend>
          <outbound>
            <base />
          </outbound>
          <on-error>
            <base />
          </on-error>
        </policies>
      XML
    }
  }
  depends_on = [azurerm_api_management_named_value.ids]
}
