variable "subscription_id" {
  description = "Azure subscription to deploy into."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group to create for the gateway."
  type        = string
  default     = "rg-agent-gateway"
}

variable "location" {
  description = "Azure region (must offer APIM Standard v2 and your models)."
  type        = string
  default     = "eastus2"
}

variable "prefix" {
  description = "Short prefix for resource names (lowercase letters and digits)."
  type        = string
  default     = "agw"
  validation {
    condition     = can(regex("^[a-z0-9]{3,12}$", var.prefix))
    error_message = "prefix must be 3-12 lowercase letters or digits."
  }
}

variable "entra_name_prefix" {
  description = "Display-name prefix for the Entra app registrations."
  type        = string
  default     = "agent-gateway"
}

variable "publisher_email" {
  description = "Publisher email shown in the APIM developer portal (required by APIM)."
  type        = string
}

variable "publisher_name" {
  type    = string
  default = "Agent gateway"
}

variable "model_deployments" {
  description = "Azure OpenAI deployments. capacity is in thousands of tokens per minute and must fit your quota."
  type = list(object({
    name     = string
    model    = string
    version  = string
    sku      = string
    capacity = number
  }))
  default = [{
    name     = "gpt-5-mini"
    model    = "gpt-5-mini"
    version  = "2025-08-07"
    sku      = "GlobalStandard"
    capacity = 500
  }]
}

variable "agents_policy_file" {
  description = "Front-door policy rendered by render_policies.py from your gateway.yaml."
  type        = string
  default     = "../policies/agents-api.xml"
}

variable "models_policy_file" {
  description = "Model-route policy rendered by render_policies.py from your gateway.yaml."
  type        = string
  default     = "../policies/models-api.xml"
}

variable "gateway_config_file" {
  description = "gateway.yaml whose tier/team/agent groups are assigned to the gateway app (see assign_config_groups)."
  type        = string
  default     = "../gateway.example.yaml"
}

variable "assign_config_groups" {
  description = "Assign every group in gateway_config_file to the gateway app (needs Entra ID P1). Off for the example file, whose ids are placeholders."
  type        = bool
  default     = false
}

variable "preauthorize_azure_cli" {
  description = "Let the Azure CLI get gateway and chat tokens without consent prompts (developer convenience)."
  type        = bool
  default     = true
}

variable "runtime_image" {
  description = "Runtime image, e.g. <registry>.azurecr.io/agw-runtime:0.3.0. Empty on the first apply (the registry doesn't exist yet)."
  type        = string
  default     = ""
}

variable "chat_image" {
  description = "Web chat image. Empty: no web chat."
  type        = string
  default     = ""
}

variable "teams_image" {
  description = "Teams bot image. Empty: no Teams bot."
  type        = string
  default     = ""
}

variable "teams_sso_unique_id" {
  description = "Unique identifier shared by the Bot OAuth connection and its federated credential."
  type        = string
  default     = "agw-teams-sso"
}

variable "tenant_id_base64url" {
  description = <<-EOT
    The tenant id as base64url of its .NET byte order, needed in the Bot token store
    federated credential subject (Terraform can't compute it). Get it with:
    python3 -c "import uuid,base64,sys;print(base64.urlsafe_b64encode(uuid.UUID(sys.argv[1]).bytes_le).decode().rstrip('='))" <tenant-id>
  EOT
  type        = string
  default     = ""
  validation {
    condition     = var.tenant_id_base64url == "" || can(regex("^[A-Za-z0-9_-]{22}$", var.tenant_id_base64url))
    error_message = "tenant_id_base64url must be 22 base64url characters."
  }
}

variable "capture_content" {
  description = "Record user input and tool arguments (redacted where sensitive) as span events, kept 30 days."
  type        = bool
  default     = true
}

variable "sec_user_agent" {
  description = "SEC requires a User-Agent with a contact address for EDGAR requests."
  type        = string
  default     = "Your Org security@your-org.example"
}

variable "tags" {
  type    = map(string)
  default = { workload = "agent-gateway" }
}

variable "enable_copilot_studio" {
  description = "Create the OAuth client app Copilot Studio's A2A connection signs users in with."
  type        = bool
  default     = false
}

variable "copilot_studio_redirect_uris" {
  description = "Redirect URIs for the Copilot Studio OAuth client; add the one Copilot Studio shows when you create the connection."
  type        = list(string)
  default     = ["https://global.consent.azure-apim.net/redirect"]
}

variable "foundry_users" {
  description = "Object IDs of users or groups who work in the Foundry portal for this gateway (agent Monitor, Insights scans, evaluations). They get Foundry User on the AI Services account; the identity running Terraform always does."
  type        = list(string)
  default     = []
}
