#!/usr/bin/env bash
# Microsoft Entra setup for the agent gateway. Read before running.
#
#   ./entra.sh app                         create the "agent-gateway" app registration
#   ./entra.sh group <app-id> <name>       create a security group and assign it to the app
#   ./entra.sh grant <app-id> <principal-id>...
#                                          give managed identities the Gateway.Internal role
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

case "${1:-}" in
  app) cmd_app ;;
  group) [[ $# -eq 3 ]] || { echo "usage: $0 group <app-id> <group-name>" >&2; exit 2; }; cmd_group "$2" "$3" ;;
  grant) [[ $# -ge 3 ]] || { echo "usage: $0 grant <app-id> <principal-id>..." >&2; exit 2; }; shift; cmd_grant "$@" ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
