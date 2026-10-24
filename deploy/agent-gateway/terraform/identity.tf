# User-assigned identities: their client ids are known up front, which the policies,
# Container Apps auth and federated credentials all need.

resource "azurerm_user_assigned_identity" "apim" {
  name                = "id-${var.prefix}-apim"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "runtime" {
  name                = "id-${var.prefix}-runtime"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  tags                = var.tags
}

# Web chat: pulls its image and is the chat app's client credential for OBO (no secret).
resource "azurerm_user_assigned_identity" "chat" {
  name                = "id-${var.prefix}-chat"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  tags                = var.tags
}

# Teams bot: pulls its image and is the bot app's credential for Bot Service and OBO.
resource "azurerm_user_assigned_identity" "bot" {
  name                = "id-${var.prefix}-bot"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  tags                = var.tags
}
