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

@description('SEC requires a User-Agent with a contact address for EDGAR requests.')
param secUserAgent string = 'Your Org security@your-org.example'

@description('Front-door policy XML rendered from your gateway.yaml. Defaults to the example config.')
param agentsPolicyXml string = loadTextContent('../policies/agents-api.xml')

@description('Model-route policy XML rendered from your gateway.yaml. Defaults to the example config.')
param modelsPolicyXml string = loadTextContent('../policies/models-api.xml')

var suffix = uniqueString(resourceGroup().id)
var deployRuntime = !empty(runtimeImage)
var gatewayAudience = 'api://${gatewayAppId}'

// Built-in role definition ids
var roles = {
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  eventHubsSender: '2b629674-e913-4c01-ae53-ef4638d8f975' // Azure Event Hubs Data Sender
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

resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'ais-${prefix}-${suffix}'
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: 'ais-${prefix}-${suffix}'
    disableLocalAuth: true // Entra only: APIM's identity is the sole caller
    publicNetworkAccess: 'Enabled'
  }
}

@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [
  for d in modelDeployments: {
    parent: foundry
    name: d.name
    sku: { name: d.sku, capacity: d.capacity }
    properties: {
      model: { format: 'OpenAI', name: d.model, version: d.version }
    }
  }
]

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

// Dashboard: Azure portal > Monitor > Workbooks > "Agent gateway".
resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'agent-gateway-workbook')
  location: location
  kind: 'shared'
  properties: {
    displayName: 'Agent gateway'
    category: 'workbook'
    sourceId: logs.id
    serializedData: string(union(loadJsonContent('workbook.json'), { fallbackResourceIds: [logs.id] }))
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
            { name: 'AZURE_CLIENT_ID', value: runtimeIdentity.properties.clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            { name: 'OTEL_SERVICE_NAME', value: 'agent-gateway-runtime' }
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

resource agentsPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = if (deployRuntime) {
  parent: agentsApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: agentsPolicyXml
  }
  dependsOn: [nvTenant, nvGatewayApp, nvApimClient, nvRuntimeClient, runtimeBackend]
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
output workbookId string = workbook.id
