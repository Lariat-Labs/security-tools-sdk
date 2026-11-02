# Optional: Entra security groups for gateway.yaml's tiers, teams and agents.
#
# gateway.yaml holds group object ids, and the APIM policies are rendered from it, so the
# groups must exist before the policies can be rendered:
#
#   1. set gateway_groups, then: terraform apply -target=azuread_group.gateway
#   2. copy `terraform output gateway_group_ids` into your gateway.yaml
#   3. render_policies.py, set assign_config_groups = true, then terraform apply
#
# Leave gateway_groups empty to bring your own groups. Either way, assigning them to the
# gateway app (assign_config_groups) is what puts them in users' tokens.

resource "azuread_group" "gateway" {
  for_each                = var.gateway_groups
  display_name            = each.value.display_name
  description             = each.value.description
  security_enabled        = true
  prevent_duplicate_names = true
  owners                  = [data.azurerm_client_config.current.object_id]
  # Direct membership only: transitive members aren't in tokens for app-assigned groups.
  members = distinct(concat(
    each.value.members,
    each.value.include_deployer ? [data.azurerm_client_config.current.object_id] : [],
  ))
}
