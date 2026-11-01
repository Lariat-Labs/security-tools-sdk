# Quick commands. `make` lists them.
#
# Terraform targets act on deploy/agent-gateway/terraform (local state + terraform.tfvars).
# They never auto-approve: Terraform shows the plan and asks before changing anything.

TF   := terraform -chdir=deploy/agent-gateway/terraform
AGW  := deploy/agent-gateway
# Gateway URL and app id from Terraform outputs, unless already set in the environment.
GW_ENV := AGW_GATEWAY_URL=$${AGW_GATEWAY_URL:-$$($(TF) output -raw gateway_url)} \
          AGW_GATEWAY_APP_ID=$${AGW_GATEWAY_APP_ID:-$$($(TF) output -raw gateway_app_id)}
AGENT ?= cve-triage
Q     ?= Prioritise CVE-2024-3400 and CVE-2023-4966

.DEFAULT_GOAL := help
.PHONY: help sync test lint fmt typecheck docs check \
        webui webui-local runtime runtime-build gw-me gw-list gw-run local-run \
        tf-init tf-plan tf-apply apim-down apim-up down

help: ## List the commands
	@awk 'BEGIN {FS = ":.*## "} /^##@/ {printf "\n%s\n", substr($$0, 5)} \
	  /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Development
sync: ## Install every dependency group
	uv sync --all-groups

test: ## Run the offline test suite
	uv run pytest -q

lint: ## Ruff lint and format check
	uv run ruff check . && uv run ruff format --check .

fmt: ## Ruff autofix and format
	uv run ruff check --fix . && uv run ruff format .

typecheck: ## mypy over the SDK and CLI
	uv run mypy packages/secsdk/src packages/secctl/src

docs: ## Build the docs (strict)
	uv run --group docs mkdocs build --strict

check: lint typecheck test docs ## Everything CI cares about

##@ Agent gateway: local
webui: ## Open WebUI on :3000 through the deployed gateway, as you (az login)
	uv run python $(AGW)/run_webui.py

webui-local: ## Open WebUI on :3000 through the local runtime (start `make runtime` first)
	uv run python $(AGW)/run_webui.py --local

runtime: ## Run the runtime container on :8000 against Foundry
	uv run python $(AGW)/run_local.py

runtime-build: ## Rebuild the runtime image, then run it
	uv run python $(AGW)/run_local.py --build

gw-me: ## Your gateway profile: tier, team, limits
	$(GW_ENV) uv run python $(AGW)/client.py me

gw-list: ## Agents you may use through the gateway
	$(GW_ENV) uv run python $(AGW)/client.py list

gw-run: ## Run an agent through the gateway: make gw-run AGENT=cve-triage Q="..."
	$(GW_ENV) uv run python $(AGW)/client.py run $(AGENT) "$(Q)"

local-run: ## Run an agent on the local runtime: make local-run AGENT=... Q="..."
	uv run python $(AGW)/client.py --local run $(AGENT) "$(Q)"

##@ Agent gateway: Azure (Terraform)
tf-init: ## terraform init
	$(TF) init

tf-plan: ## Show what Terraform would change
	$(TF) plan

tf-apply: ## Apply (also brings APIM and the apps back after apim-down)
	$(TF) apply

apim-down: ## Destroy APIM and what depends on it (container apps, bot); asks first
	@echo "Destroys APIM plus what references it: the container apps, Azure Bot and the chat app's redirect URIs (32 resources)."
	@echo "Foundry, monitoring, Event Hubs, registry and Entra stay. 'make tf-apply' restores it."
	$(TF) destroy -target=azurerm_api_management.this

apim-up: tf-apply ## Same as tf-apply

down: ## Destroy EVERYTHING in Terraform, Entra apps and groups included; asks first
	@echo "Destroys every resource in this Terraform state, including Entra app registrations"
	@echo "and groups, and purges the soft-deleted APIM and AI Services account."
	$(TF) destroy
