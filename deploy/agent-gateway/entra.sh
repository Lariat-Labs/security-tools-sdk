#!/usr/bin/env bash
# Microsoft Entra setup for the agent gateway. Read before running.
#
#   ./entra.sh app                         create the "agent-gateway" app registration
#   ./entra.sh group <app-id> <name>       create a security group and assign it to the app
#   ./entra.sh grant <app-id> <principal-id>...
#                                          give managed identities the Gateway.Internal role
#   ./entra.sh chat <gateway-app-id>       create the "agent-gateway-chat" app (web chat, OBO)
#   ./entra.sh chat-link <chat-app-id> <chat-url> <chat-mi-principal-id>
#                                          add the redirect URI and trust the chat's managed identity
#   ./entra.sh teams <gateway-app-id> <bot-mi-principal-id>
#                                          create the "agent-gateway-teams" bot app (Teams SSO, OBO)
#
# Needs: az login as an Application Administrator (or Cloud Application Administrator).
# Assigning groups to an application needs Microsoft Entra ID P1 or P2.
#
# What "app" creates:
#   * identifier URI api://<app-id>, v2 access tokens
#   * delegated scope Agents.Invoke (what users' and clients' tokens carry)
#   * app role Agents.User (assigned to tier/team/agent groups) and app role
#     Gateway.Internal (assigned to the APIM and runtime managed identities)
#   * groupMembershipClaims = ApplicationGroup: tokens list only the groups assigned to
#     this app, which keeps tokens small (no group overage) and leaks no other groups
#   * appRoleAssignmentRequired = true: only assigned users and groups can get a token
#   * Azure CLI pre-authorised for Agents.Invoke, so `az account get-access-token` works
set -euo pipefail

APP_NAME="${AGW_APP_NAME:-agent-gateway}"
AZURE_CLI_APP_ID="04b07795-8ddb-461a-bbee-02f9e1bf7b46"
GRAPH="https://graph.microsoft.com/v1.0"

new_id() { python3 -c 'import uuid; print(uuid.uuid4())'; }

api_body() { # $1 scope id, $2 "true" to include the Azure CLI pre-authorisation
  local preauth="[]"
  if [[ "$2" == "true" ]]; then
    preauth="[{\"appId\": \"$AZURE_CLI_APP_ID\", \"delegatedPermissionIds\": [\"$1\"]}]"
  fi
  cat <<JSON
{
  "requestedAccessTokenVersion": 2,
  "oauth2PermissionScopes": [{
    "id": "$1", "value": "Agents.Invoke", "type": "User", "isEnabled": true,
    "adminConsentDisplayName": "Run agents",
    "adminConsentDescription": "Run agents through the agent gateway as the signed-in user.",
    "userConsentDisplayName": "Run agents",
    "userConsentDescription": "Run agents through the agent gateway as you."
  }],
  "preAuthorizedApplications": $preauth
}
JSON
}

cmd_app() {
  local app_id obj_id scope_id user_role internal_role
  app_id=$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)
  obj_id=$(az ad app show --id "$app_id" --query id -o tsv)
  scope_id=$(new_id); user_role=$(new_id); internal_role=$(new_id)

  az rest --method PATCH --url "$GRAPH/applications/$obj_id" --headers Content-Type=application/json --body @- <<JSON
{
  "identifierUris": ["api://$app_id"],
  "groupMembershipClaims": "ApplicationGroup",
  "api": $(api_body "$scope_id" false),
  "appRoles": [
    {"id": "$user_role", "value": "Agents.User", "displayName": "Agent user",
     "description": "Members may use the agent gateway.", "allowedMemberTypes": ["User"], "isEnabled": true},
    {"id": "$internal_role", "value": "Gateway.Internal", "displayName": "Gateway internal",
     "description": "APIM and the agent runtime calling each other.", "allowedMemberTypes": ["Application"], "isEnabled": true}
  ]
}
JSON
  # The scope must exist before it can be pre-authorised.
  az rest --method PATCH --url "$GRAPH/applications/$obj_id" --headers Content-Type=application/json \
    --body "{\"api\": $(api_body "$scope_id" true)}"

  az ad sp create --id "$app_id" --query id -o tsv >/dev/null
  az ad sp update --id "$app_id" --set appRoleAssignmentRequired=true

  echo "gatewayAppId=$app_id"
  echo "Audience: api://$app_id   User scope: api://$app_id/Agents.Invoke"
  echo "Next: create groups with './entra.sh group $app_id <name>' and put their ids in gateway.yaml."
}

cmd_group() {
  local app_id="$1" name="$2" sp_id role_id group_id
  sp_id=$(az ad sp show --id "$app_id" --query id -o tsv)
  role_id=$(az ad sp show --id "$app_id" --query "appRoles[?value=='Agents.User'].id | [0]" -o tsv)
  group_id=$(az ad group create --display-name "$name" --mail-nickname "$name" --query id -o tsv)
  az rest --method POST --url "$GRAPH/servicePrincipals/$sp_id/appRoleAssignedTo" \
    --headers Content-Type=application/json \
    --body "{\"principalId\": \"$group_id\", \"resourceId\": \"$sp_id\", \"appRoleId\": \"$role_id\"}" >/dev/null
  echo "$name group_id: $group_id"
}

cmd_grant() {
  local app_id="$1"; shift
  local sp_id role_id
  sp_id=$(az ad sp show --id "$app_id" --query id -o tsv)
  role_id=$(az ad sp show --id "$app_id" --query "appRoles[?value=='Gateway.Internal'].id | [0]" -o tsv)
  for principal in "$@"; do
    az rest --method POST --url "$GRAPH/servicePrincipals/$sp_id/appRoleAssignedTo" \
      --headers Content-Type=application/json \
      --body "{\"principalId\": \"$principal\", \"resourceId\": \"$sp_id\", \"appRoleId\": \"$role_id\"}" >/dev/null
    echo "Gateway.Internal -> $principal"
  done
}

cmd_chat() {
  # The chat app is both the SPA users sign in to and the API the SPA calls (access_as_user).
  # Its backend exchanges that token for a gateway token (on-behalf-of). It has no secret:
  # chat-link adds a federated credential that trusts the chat's managed identity.
  local gateway_app="$1" chat_app obj_id scope_id gateway_scope
  gateway_scope=$(az ad app show --id "$gateway_app" --query "api.oauth2PermissionScopes[?value=='Agents.Invoke'].id | [0]" -o tsv)
  chat_app=$(az ad app create --display-name "${AGW_CHAT_APP_NAME:-agent-gateway-chat}" --sign-in-audience AzureADMyOrg --query appId -o tsv)
  obj_id=$(az ad app show --id "$chat_app" --query id -o tsv)
  scope_id=$(new_id)
  az rest --method PATCH --url "$GRAPH/applications/$obj_id" --headers Content-Type=application/json --body @- <<JSON
{
  "identifierUris": ["api://$chat_app"],
  "api": {
    "requestedAccessTokenVersion": 2,
    "oauth2PermissionScopes": [{
      "id": "$scope_id", "value": "access_as_user", "type": "User", "isEnabled": true,
      "adminConsentDisplayName": "Use the agent chat", "adminConsentDescription": "Use the agent gateway chat as the signed-in user.",
      "userConsentDisplayName": "Use the agent chat", "userConsentDescription": "Use the agent gateway chat as you."
    }]
  }
}
JSON
  az rest --method PATCH --url "$GRAPH/applications/$obj_id" --headers Content-Type=application/json --body @- <<JSON
{
  "requiredResourceAccess": [
    {"resourceAppId": "$gateway_app", "resourceAccess": [{"id": "$gateway_scope", "type": "Scope"}]},
    {"resourceAppId": "$chat_app", "resourceAccess": [{"id": "$scope_id", "type": "Scope"}]}
  ]
}
JSON
  az ad sp create --id "$chat_app" --query id -o tsv >/dev/null
  # Tenant-wide consent, so users aren't prompted (and OBO, which can't prompt, works).
  # Explicit grants: `az ad app permission admin-consent` can silently miss a brand-new app.
  az ad app permission grant --id "$chat_app" --api "$gateway_app" --scope Agents.Invoke -o none
  az ad app permission grant --id "$chat_app" --api "$chat_app" --scope access_as_user -o none
  echo "chatAppId=$chat_app"
  echo "Next: deploy with chatAppId, then './entra.sh chat-link $chat_app <chatUrl> <chatPrincipalId>'."
}

cmd_chat_link() {
  local chat_app="$1" url="${2%/}" principal="$3" obj_id tenant
  obj_id=$(az ad app show --id "$chat_app" --query id -o tsv)
  tenant=$(az account show --query tenantId -o tsv)
  az rest --method PATCH --url "$GRAPH/applications/$obj_id" --headers Content-Type=application/json \
    --body "{\"spa\": {\"redirectUris\": [\"$url/\", \"http://localhost:8000/\"]}}"
  az rest --method POST --url "$GRAPH/applications/$obj_id/federatedIdentityCredentials" \
    --headers Content-Type=application/json --body @- >/dev/null <<JSON
{
  "name": "agw-chat-managed-identity",
  "issuer": "https://login.microsoftonline.com/$tenant/v2.0",
  "subject": "$principal",
  "audiences": ["api://AzureADTokenExchange"],
  "description": "The chat backend's managed identity is this app's client credential (no secret)."
}
JSON
  echo "Redirect URI $url/ added; managed identity $principal trusted as the client credential."
}

cmd_teams() {
  # One app registration is the bot's identity AND its Teams SSO app (api://botid-<appId>).
  # No secrets: two federated credentials let (1) the Azure Bot token store run the OAuth
  # connection and (2) the bot's managed identity authenticate as the app and do OBO.
  local gateway_app="$1" bot_mi="$2" app obj scope_id gateway_scope tenant tenant_b64 unique
  unique="${AGW_TEAMS_SSO_ID:-agw-teams-sso}"
  tenant=$(az account show --query tenantId -o tsv)
  # Base64url of the tenant GUID in .NET byte order, as the Bot token store presents it.
  tenant_b64=$(python3 -c "import uuid,base64,sys; print(base64.urlsafe_b64encode(uuid.UUID(sys.argv[1]).bytes_le).decode().rstrip('='))" "$tenant")
  gateway_scope=$(az ad app show --id "$gateway_app" --query "api.oauth2PermissionScopes[?value=='Agents.Invoke'].id | [0]" -o tsv)
  app=$(az ad app create --display-name "${AGW_TEAMS_APP_NAME:-agent-gateway-teams}" --sign-in-audience AzureADMyOrg \
        --web-redirect-uris "https://token.botframework.com/.auth/web/redirect" --query appId -o tsv)
  obj=$(az ad app show --id "$app" --query id -o tsv)
  scope_id=$(new_id)
  teams_api() { # $1 = include Teams client pre-authorisation
    local pre="[]"
    if [[ "$1" == "true" ]]; then
      pre="[{\"appId\": \"1fec8e78-bce4-4aaf-ab1b-5451cc387264\", \"delegatedPermissionIds\": [\"$scope_id\"]},
            {\"appId\": \"5e3ce6c0-2b1f-4285-8d4b-75ee78787346\", \"delegatedPermissionIds\": [\"$scope_id\"]}]"
    fi
    echo "{\"requestedAccessTokenVersion\": 2, \"oauth2PermissionScopes\": [{\"id\": \"$scope_id\", \"value\": \"defaultScopes\", \"type\": \"User\", \"isEnabled\": true,
      \"adminConsentDisplayName\": \"Use the agent gateway bot\", \"adminConsentDescription\": \"Use the agent gateway bot as the signed-in user.\",
      \"userConsentDisplayName\": \"Use the agent gateway bot\", \"userConsentDescription\": \"Use the agent gateway bot as you.\"}],
      \"preAuthorizedApplications\": $pre}"
  }
  az rest --method PATCH --url "$GRAPH/applications/$obj" --headers Content-Type=application/json \
    --body "{\"identifierUris\": [\"api://botid-$app\"], \"api\": $(teams_api false)}"
  # Teams desktop/mobile and web may get SSO tokens without a consent prompt.
  az rest --method PATCH --url "$GRAPH/applications/$obj" --headers Content-Type=application/json \
    --body "{\"api\": $(teams_api true), \"requiredResourceAccess\": [
      {\"resourceAppId\": \"$gateway_app\", \"resourceAccess\": [{\"id\": \"$gateway_scope\", \"type\": \"Scope\"}]},
      {\"resourceAppId\": \"$app\", \"resourceAccess\": [{\"id\": \"$scope_id\", \"type\": \"Scope\"}]}]}"
  az ad sp create --id "$app" -o none
  az ad app permission grant --id "$app" --api "$gateway_app" --scope Agents.Invoke -o none
  az ad app permission grant --id "$app" --api "$app" --scope defaultScopes -o none
  for fic in \
    "{\"name\": \"bot-token-store\", \"issuer\": \"https://login.microsoftonline.com/$tenant/v2.0\", \"subject\": \"/eid1/c/pub/t/$tenant_b64/a/9ExAW52n_ky4ZiS_jhpJIQ/$unique\", \"audiences\": [\"api://AzureADTokenExchange\"], \"description\": \"Azure Bot OAuth connection (AAD v2 with Federated Credentials)\"}" \
    "{\"name\": \"bot-managed-identity\", \"issuer\": \"https://login.microsoftonline.com/$tenant/v2.0\", \"subject\": \"$bot_mi\", \"audiences\": [\"api://AzureADTokenExchange\"], \"description\": \"The bot's managed identity is this app's credential\"}"; do
    az rest --method POST --url "$GRAPH/applications/$obj/federatedIdentityCredentials" \
      --headers Content-Type=application/json --body "$fic" -o none
  done
  echo "teamsAppId=$app"
  echo "teamsSsoUniqueId=$unique"
}

case "${1:-}" in
  app) cmd_app ;;
  group) [[ $# -eq 3 ]] || { echo "usage: $0 group <app-id> <group-name>" >&2; exit 2; }; cmd_group "$2" "$3" ;;
  grant) [[ $# -ge 3 ]] || { echo "usage: $0 grant <app-id> <principal-id>..." >&2; exit 2; }; shift; cmd_grant "$@" ;;
  chat) [[ $# -eq 2 ]] || { echo "usage: $0 chat <gateway-app-id>" >&2; exit 2; }; cmd_chat "$2" ;;
  chat-link) [[ $# -eq 4 ]] || { echo "usage: $0 chat-link <chat-app-id> <chat-url> <chat-mi-principal-id>" >&2; exit 2; }; cmd_chat_link "$2" "$3" "$4" ;;
  teams) [[ $# -eq 3 ]] || { echo "usage: $0 teams <gateway-app-id> <bot-mi-principal-id>" >&2; exit 2; }; cmd_teams "$2" "$3" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
