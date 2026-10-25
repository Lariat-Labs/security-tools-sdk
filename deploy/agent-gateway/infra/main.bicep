// Agent gateway: APIM (Standard v2) in front of an agent runtime (Container Apps) and
// Azure OpenAI model deployments, with token limits per user, team, agent and deployment.
//
// Two-step deploy (see docs/agent-gateway.md):
//   1. runtimeImage = '' : creates everything except the Container App and the agents API.
//   2. Build the image into the registry, then redeploy with runtimeImage set.
//
// Prerequisite: the "agent-gateway" Entra app registration (deploy/agent-gateway/entra.sh).
// Deploying needs Owner or Contributor + User Access Administrator on the resource group
// (the template assigns roles to managed identities).

targetScope = 'resourceGroup'

@description('Short prefix for resource names (lowercase letters and digits).')
@minLength(3)
@maxLength(12)
param prefix string = 'agw'

param location string = resourceGroup().location

@description('Publisher email shown in the APIM developer portal (required by APIM).')
param publisherEmail string

param publisherName string = 'Agent gateway'

@description('Application (client) id of the agent-gateway Entra app registration.')
param gatewayAppId string

param tenantId string = tenant().tenantId

@description('Azure OpenAI deployments. capacity is in thousands of tokens per minute and must fit your approved quota.')
param modelDeployments array = [
  {
    name: 'gpt-5-mini'
    model: 'gpt-5-mini'
    version: '2025-08-07'
    sku: 'GlobalStandard'
    capacity: 500
  }
]

@description('Runtime image, e.g. <registry>.azurecr.io/agw-runtime:0.1.0. Leave empty on the first deploy.')
param runtimeImage string = ''

@description('Client id of the agent-gateway-chat app registration (entra.sh chat). Empty: no chat app.')
param chatAppId string = ''

@description('Chat image, e.g. <registry>.azurecr.io/agw-chat:0.1.0. Needs chatAppId.')
param chatImage string = ''

@description('Record user input and tool arguments (redacted where sensitive) as span events, kept 30 days.')
param captureContent bool = true

@description('App registration of the Teams bot (entra.sh teams). Empty: no Teams bot.')
param teamsAppId string = ''

@description('Teams bot image, e.g. <registry>.azurecr.io/agw-teams:0.1.0. Needs teamsAppId.')
param teamsImage string = ''

@description('Unique identifier in the bot token store federated credential (entra.sh teams).')
param teamsSsoUniqueId string = 'agw-teams-sso'

@description('SEC requires a User-Agent with a contact address for EDGAR requests.')
param secUserAgent string = 'Your Org security@your-org.example'

@description('Front-door policy XML rendered from your gateway.yaml. Defaults to the example config.')
param agentsPolicyXml string = loadTextContent('../policies/agents-api.xml')

@description('Model-route policy XML rendered from your gateway.yaml. Defaults to the example config.')
param modelsPolicyXml string = loadTextContent('../policies/models-api.xml')

var suffix = uniqueString(resourceGroup().id)
var deployRuntime = !empty(runtimeImage)
var deployChat = deployRuntime && !empty(chatAppId) && !empty(chatImage)
var deployTeams = deployRuntime && !empty(teamsAppId) && !empty(teamsImage)
var gatewayAudience = 'api://${gatewayAppId}'

// Built-in role definition ids
var roles = {
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  eventHubsSender: '2b629674-e913-4c01-ae53-ef4638d8f975' // Azure Event Hubs Data Sender
  monitoringReader: '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
}

// ----- identities ---------------------------------------------------------------------------

// User-assigned so their client ids are known here (policies and Easy Auth need them).
resource apimIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-apim'
  location: location
}

resource runtimeIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-runtime'
  location: location
}

// The web chat's identity: pulls its image, and is the chat app registration's client
// credential for the on-behalf-of exchange (federated identity credential, no secret).
resource chatIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-chat'
  location: location
}

// The Teams bot's identity: pulls its image and is the bot app registration's credential
// (federated identity credential) for Bot Service and for the OBO exchange. No secret.
resource botIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-bot'
  location: location
}

// ----- monitoring -----------------------------------------------------------------------------

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 90
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${prefix}-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// Usage events (one per model call) for chargeback and the data lake.
resource eventHubs 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: 'evhns-${prefix}-${suffix}'
  location: location
  sku: { name: 'Standard', tier: 'Standard', capacity: 1 }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
  }
}

resource usageHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: eventHubs
  name: 'agw-usage'
  properties: {
    partitionCount: 2
    messageRetentionInDays: 7
  }
}

resource apimSendsUsage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: usageHub
  name: guid(usageHub.id, apimIdentity.id, roles.eventHubsSender)
  properties: {
    principalId: apimIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.eventHubsSender)
  }
}

// ----- models ---------------------------------------------------------------------------------

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'ais-${prefix}-${suffix}'
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true // Foundry projects (trace view for the external agents)
    customSubDomainName: 'ais-${prefix}-${suffix}'
    disableLocalAuth: true // Entra only: APIM's identity is the sole caller
    publicNetworkAccess: 'Enabled'
  }
}

@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [
  for d in modelDeployments: {
    parent: foundry
    name: d.name
    sku: { name: d.sku, capacity: d.capacity }
    properties: {
      model: { format: 'OpenAI', name: d.model, version: d.version }
    }
  }
]

// Foundry project whose agent trace view shows the runtime's traces. The agents are
// registered as "external" agents (deploy/agent-gateway/register_agents.py); Foundry
// matches spans to them by gen_ai.agent.id.
resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: 'agents'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Agent gateway'
    description: 'Observability and evaluation for the agent gateway runtime.'
  }
  // One operation at a time on the account (it rejects concurrent updates).
  dependsOn: [deployments]
}

// Foundry's agent Monitor tab queries Application Insights as the project's managed identity.
resource projectReadsTelemetry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, foundryProject.id, roles.monitoringReader)
  properties: {
    principalId: foundryProject.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringReader)
  }
}

resource foundryAppInsights 'Microsoft.CognitiveServices/accounts/connections@2025-06-01' = {
  parent: foundry
  name: 'appinsights'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: { key: appInsights.properties.ConnectionString }
    metadata: { ApiType: 'Azure', ResourceId: appInsights.id }
  }
  dependsOn: [foundryProject]
}

resource apimCallsModels 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, apimIdentity.id, roles.openAiUser)
  properties: {
    principalId: apimIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser)
  }
}

// ----- API Management -------------------------------------------------------------------------

resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' = {
  name: 'apim-${prefix}-${suffix}'
  location: location
  sku: { name: 'StandardV2', capacity: 1 }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${apimIdentity.id}': {} }
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

// Environment-specific ids referenced by the rendered policies as {{agw-...}}.
resource nvTenant 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'agw-tenant-id'
  properties: { displayName: 'agw-tenant-id', value: tenantId }
}

resource nvGatewayApp 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'agw-gateway-app-id'
  properties: { displayName: 'agw-gateway-app-id', value: gatewayAppId }
}

resource nvApimClient 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'agw-apim-client-id'
  properties: { displayName: 'agw-apim-client-id', value: apimIdentity.properties.clientId }
}

resource nvRuntimeClient 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'agw-runtime-client-id'
  properties: { displayName: 'agw-runtime-client-id', value: runtimeIdentity.properties.clientId }
}

resource appInsightsLogger 'Microsoft.ApiManagement/service/loggers@2024-06-01-preview' = {
  parent: apim
  name: 'appinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsights.id
    credentials: { connectionString: appInsights.properties.ConnectionString }
  }
}

// metrics: true enables custom metrics with dimensions (llm-emit-token-metric).
resource appInsightsDiagnostic 'Microsoft.ApiManagement/service/diagnostics@2024-06-01-preview' = {
  parent: apim
  name: 'applicationinsights'
  properties: {
    loggerId: appInsightsLogger.id
    alwaysLog: 'allErrors'
    sampling: { samplingType: 'fixed', percentage: 100 }
    verbosity: 'information'
    metrics: true
    logClientIp: false
    httpCorrelationProtocol: 'W3C'
  }
}

resource usageLogger 'Microsoft.ApiManagement/service/loggers@2024-06-01-preview' = {
  parent: apim
  name: 'agw-usage'
  properties: {
    loggerType: 'azureEventHub'
    credentials: {
      endpointAddress: '${eventHubs.name}.servicebus.windows.net'
      identityClientId: apimIdentity.properties.clientId
      name: usageHub.name
    }
  }
  dependsOn: [apimSendsUsage]
}

// Gateway and LLM logs (token counts per request; no prompt text) to Log Analytics.
resource apimDiagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: apim
  name: 'to-log-analytics'
  properties: {
    workspaceId: logs.id
    logAnalyticsDestinationType: 'Dedicated'
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

// Token counts per model call in ApiManagementGatewayLlmLog.
resource llmLogs 'Microsoft.ApiManagement/service/diagnostics@2024-06-01-preview' = {
  parent: apim
  name: 'azuremonitor'
  properties: {
    loggerId: '/loggers/azuremonitor'
    // Prompt and completion logging are separate opt-ins (requests/responses); left off.
    largeLanguageModel: { logs: 'enabled' }
  }
  dependsOn: [apimDiagnosticSettings]
}

// Dashboards: Azure portal > Monitor > Workbooks. Generated by workbooks/build.py.
var workbooks = [
  { key: 'overview', name: 'Agent gateway', data: loadJsonContent('workbooks/overview.json') }
  { key: 'run-explorer', name: 'Agent gateway: run explorer', data: loadJsonContent('workbooks/run-explorer.json') }
  { key: 'tools', name: 'Agent gateway: tools', data: loadJsonContent('workbooks/tools.json') }
]

resource dashboards 'Microsoft.Insights/workbooks@2023-06-01' = [
  for wb in workbooks: {
    // The overview keeps the id it was first deployed with.
    name: wb.key == 'overview' ? guid(resourceGroup().id, 'agent-gateway-workbook') : guid(resourceGroup().id, 'agent-gateway-workbook', wb.key)
    location: location
    kind: 'shared'
    properties: {
      displayName: wb.name
      category: 'workbook'
      sourceId: logs.id
      serializedData: replace(string(union(wb.data, { fallbackResourceIds: [logs.id] })), '__AGW_WORKSPACE_ID__', logs.id)
    }
  }
]

// Captured content (user input, tool arguments) is stored as span events in AppTraces:
// keep it 30 days. Spans (AppDependencies) keep the workspace default of 90.
resource contentRetention 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = {
  parent: logs
  name: 'AppTraces'
  properties: {
    retentionInDays: 30
    totalRetentionInDays: 30
  }
}

resource modelsBackend 'Microsoft.ApiManagement/service/backends@2024-06-01-preview' = {
  parent: apim
  name: 'agw-models'
  properties: {
    protocol: 'http'
    url: '${foundry.properties.endpoints['OpenAI Language Model Instance API']}openai/v1'
    // Fail fast for a while when the deployment keeps throttling.
    circuitBreaker: {
      rules: [
        {
          name: 'model-throttling'
          failureCondition: {
            count: 3
            interval: 'PT1M'
            statusCodeRanges: [{ min: 429, max: 429 }]
          }
          tripDuration: 'PT1M'
          acceptRetryAfter: true
        }
      ]
    }
  }
}

// The runtime's only route to the models.
resource modelsApi 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = {
  parent: apim
  name: 'agw-models'
  properties: {
    displayName: 'Agent gateway: models'
    path: 'models/openai/v1'
    protocols: ['https']
    subscriptionRequired: false
  }
}

resource chatCompletions 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: modelsApi
  name: 'chat-completions'
  properties: {
    displayName: 'Chat completions'
    method: 'POST'
    urlTemplate: '/chat/completions'
  }
}

resource modelsPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = {
  parent: modelsApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: modelsPolicyXml
  }
  dependsOn: [nvTenant, nvGatewayApp, nvApimClient, nvRuntimeClient, modelsBackend, usageLogger]
}

// ----- agent runtime --------------------------------------------------------------------------

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'cr${prefix}${suffix}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource runtimePullsImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, runtimeIdentity.id, roles.acrPull)
  properties: {
    principalId: runtimeIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${prefix}-${suffix}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource runtime 'Microsoft.App/containerApps@2024-03-01' = if (deployRuntime) {
  name: 'ca-${prefix}-runtime'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${runtimeIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: { external: true, targetPort: 8000, transport: 'auto', allowInsecure: false }
      registries: [{ server: registry.properties.loginServer, identity: runtimeIdentity.id }]
    }
    template: {
      containers: [
        {
          name: 'runtime'
          image: runtimeImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'AGW_MODEL_BASE_URL', value: '${apim.properties.gatewayUrl}/models/openai/v1' }
            { name: 'AGW_GATEWAY_SCOPE', value: '${gatewayAudience}/.default' }
            { name: 'AGW_PUBLIC_BASE_URL', value: apim.properties.gatewayUrl }
            { name: 'AZURE_CLIENT_ID', value: runtimeIdentity.properties.clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            { name: 'OTEL_SERVICE_NAME', value: 'agent-gateway-runtime' }
            // Keep every span: the default rate-limited sampler (5 traces/s) drops spans
            // mid-run, which leaves holes in the run timeline and audit trail.
            { name: 'OTEL_TRACES_SAMPLER', value: 'always_on' }
            { name: 'AGW_CAPTURE_CONTENT', value: captureContent ? 'true' : 'false' }
            // Middle tiers that call on-behalf-of users: their runs are recorded as auth flow "obo".
            { name: 'AGW_OBO_CLIENT_IDS', value: join(filter([chatAppId, teamsAppId], id => !empty(id)), ',') }
            { name: 'SECSDK_SEC_USER_AGENT', value: secUserAgent }
            { name: 'SECSDK_TIMEOUT', value: '30' }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 } }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 5 }
    }
  }
  dependsOn: [runtimePullsImages]
}

// Only the gateway's identity may call the runtime, so its x-agw-* headers can be trusted.
resource runtimeAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = if (deployRuntime) {
  parent: runtime
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: { unauthenticatedClientAction: 'Return401' }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: gatewayAppId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [gatewayAudience, gatewayAppId]
          defaultAuthorizationPolicy: {
            allowedApplications: [apimIdentity.properties.clientId]
          }
        }
      }
    }
  }
}

resource runtimeBackend 'Microsoft.ApiManagement/service/backends@2024-06-01-preview' = if (deployRuntime) {
  parent: apim
  name: 'agw-runtime'
  properties: {
    protocol: 'http'
    url: 'https://${runtime!.properties.configuration!.ingress!.fqdn}/agents'
  }
}

// The front door users call.
resource agentsApi 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = if (deployRuntime) {
  parent: apim
  name: 'agw-agents'
  properties: {
    displayName: 'Agent gateway: agents'
    path: 'agents'
    protocols: ['https']
    subscriptionRequired: false
  }
}

resource listAgents 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = if (deployRuntime) {
  parent: agentsApi
  name: 'list-agents'
  properties: {
    displayName: 'List my agents'
    method: 'GET'
    urlTemplate: '/'
  }
}

resource whoami 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = if (deployRuntime) {
  parent: agentsApi
  name: 'whoami'
  properties: {
    displayName: 'My gateway profile (tier, team, cost center, limits)'
    method: 'GET'
    urlTemplate: '/me'
  }
}

resource runAgent 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = if (deployRuntime) {
  parent: agentsApi
  name: 'run-agent'
  properties: {
    displayName: 'Run an agent'
    method: 'POST'
    urlTemplate: '/{agent}/runs'
    templateParameters: [{ name: 'agent', type: 'string', required: true }]
  }
}

// Agent2Agent (A2A) for Copilot Studio: JSON-RPC message endpoint (user token required) and the
// public agent card (the front-door policy lets only this operation through without a token).
resource a2aMessage 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = if (deployRuntime) {
  parent: agentsApi
  name: 'a2a'
  properties: {
    displayName: 'A2A: send a message (Copilot Studio)'
    method: 'POST'
    urlTemplate: '/{agent}/a2a'
    templateParameters: [{ name: 'agent', type: 'string', required: true }]
  }
}

resource a2aCard 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = if (deployRuntime) {
  parent: agentsApi
  name: 'a2a-card'
  properties: {
    displayName: 'A2A: agent card (public)'
    method: 'GET'
    urlTemplate: '/{agent}/a2a/.well-known/{file}'
    templateParameters: [
      { name: 'agent', type: 'string', required: true }
      { name: 'file', type: 'string', required: true }
    ]
  }
}

resource agentsPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = if (deployRuntime) {
  parent: agentsApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: agentsPolicyXml
  }
  dependsOn: [nvTenant, nvGatewayApp, nvApimClient, nvRuntimeClient, runtimeBackend]
}


// ----- web chat (OBO) ------------------------------------------------------------------------

resource chatPullsImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, chatIdentity.id, roles.acrPull)
  properties: {
    principalId: chatIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource chat 'Microsoft.App/containerApps@2024-03-01' = if (deployChat) {
  name: 'ca-${prefix}-chat'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${chatIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: { external: true, targetPort: 8000, transport: 'auto', allowInsecure: false }
      registries: [{ server: registry.properties.loginServer, identity: chatIdentity.id }]
    }
    template: {
      containers: [
        {
          name: 'chat'
          image: chatImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'AGW_TENANT_ID', value: tenantId }
            { name: 'AGW_CHAT_APP_ID', value: chatAppId }
            { name: 'AGW_GATEWAY_URL', value: apim.properties.gatewayUrl }
            { name: 'AGW_GATEWAY_APP_ID', value: gatewayAppId }
            { name: 'AZURE_CLIENT_ID', value: chatIdentity.properties.clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            { name: 'OTEL_SERVICE_NAME', value: 'agent-gateway-chat' }
            // Keep every span: the default rate-limited sampler (5 traces/s) drops spans
            // mid-run, which leaves holes in the run timeline and audit trail.
            { name: 'OTEL_TRACES_SAMPLER', value: 'always_on' }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 } }
          ]
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 2 }
    }
  }
  dependsOn: [chatPullsImages]
}


// ----- Teams bot ----------------------------------------------------------------------------

resource botPullsImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, botIdentity.id, roles.acrPull)
  properties: {
    principalId: botIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource teamsApp 'Microsoft.App/containerApps@2024-03-01' = if (deployTeams) {
  name: 'ca-${prefix}-teams'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${botIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: { external: true, targetPort: 3978, transport: 'auto', allowInsecure: false }
      registries: [{ server: registry.properties.loginServer, identity: botIdentity.id }]
    }
    template: {
      containers: [
        {
          name: 'bot'
          image: teamsImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE', value: 'FederatedCredentials' }
            { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID', value: teamsAppId }
            { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID', value: tenantId }
            { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__FEDERATEDCLIENTID', value: botIdentity.properties.clientId }
            { name: 'AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GATEWAY__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME', value: 'teams-sso' }
            { name: 'AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GATEWAY__SETTINGS__OBOCONNECTIONNAME', value: 'SERVICE_CONNECTION' }
            { name: 'AGW_GATEWAY_URL', value: apim.properties.gatewayUrl }
            { name: 'AGW_GATEWAY_APP_ID', value: gatewayAppId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            { name: 'OTEL_SERVICE_NAME', value: 'agent-gateway-teams' }
            { name: 'OTEL_TRACES_SAMPLER', value: 'always_on' }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 3978 } }
          ]
        }
      ]
      // Conversation state is in memory: keep exactly one replica (see docs).
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [botPullsImages]
}

resource bot 'Microsoft.BotService/botServices@2022-09-15' = if (deployTeams) {
  name: 'bot-${prefix}-${suffix}'
  location: 'global'
  sku: { name: 'F0' }
  kind: 'azurebot'
  properties: {
    displayName: 'Agent gateway'
    endpoint: 'https://${teamsApp!.properties.configuration!.ingress!.fqdn}/api/messages'
    msaAppId: teamsAppId
    msaAppType: 'SingleTenant'
    msaAppTenantId: tenantId
    disableLocalAuth: true
  }
}

resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = if (deployTeams) {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: { isEnabled: true }
  }
}

// Teams SSO: "AAD v2 with Federated Credentials" (no client secret). The bot app
// registration trusts the Bot token store through a federated credential whose subject
// ends in teamsSsoUniqueId.
resource ssoConnection 'Microsoft.BotService/botServices/connections@2022-09-15' = if (deployTeams) {
  parent: bot
  name: 'teams-sso'
  location: 'global'
  properties: {
    serviceProviderId: 'c00b44ab-5e16-c44c-af26-2fd5bc55eb18'
    clientId: teamsAppId
    scopes: 'api://botid-${teamsAppId}/defaultScopes'
    parameters: [
      { key: 'UniqueIdentifier', value: teamsSsoUniqueId }
      { key: 'TokenExchangeUrl', value: 'api://botid-${teamsAppId}' }
      { key: 'TenantId', value: tenantId }
    ]
  }
}

// ----- outputs --------------------------------------------------------------------------------

output gatewayUrl string = apim.properties.gatewayUrl
output agentsEndpoint string = '${apim.properties.gatewayUrl}/agents'
output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output apimPrincipalId string = apimIdentity.properties.principalId
output runtimePrincipalId string = runtimeIdentity.properties.principalId
output usageEventHub string = '${eventHubs.name}/${usageHub.name}'
output appInsightsName string = appInsights.name
output logAnalyticsWorkspaceId string = logs.properties.customerId
output workbookIds array = [for (wb, i) in workbooks: dashboards[i].id]
output chatPrincipalId string = chatIdentity.properties.principalId
output chatUrl string = deployChat ? 'https://${chat!.properties.configuration!.ingress!.fqdn}' : ''
output foundryProjectEndpoint string = '${foundry.properties.endpoints['AI Foundry API']}api/projects/${foundryProject.name}'
output botPrincipalId string = botIdentity.properties.principalId
output teamsBotEndpoint string = deployTeams ? 'https://${teamsApp!.properties.configuration!.ingress!.fqdn}/api/messages' : ''
