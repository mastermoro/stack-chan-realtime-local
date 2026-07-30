targetScope = 'resourceGroup'

@description('Azure region for Container Apps resources')
param location string = resourceGroup().location

@description('Resource name prefix')
param namePrefix string = 'stackchan-rt'

@description('Container image, e.g. myregistry.azurecr.io/stackchan-relay:sha')
param containerImage string

@description('Existing Azure OpenAI / Foundry resource endpoint')
param azureOpenAIEndpoint string

@description('Realtime deployment name')
param realtimeDeployment string = 'gpt-realtime-2.1'

@description('Responses deployment name')
param responsesDeployment string

@secure()
@description('JSON map of device IDs to device tokens')
param deviceTokensJson string

@description('Optional existing Foundry/Azure OpenAI account name for RBAC')
param foundryAccountName string = ''

var logName = '${namePrefix}-logs'
var envName = '${namePrefix}-env'
var appName = '${namePrefix}-relay'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
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

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      secrets: [
        {
          name: 'device-tokens'
          value: deviceTokensJson
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'relay'
          image: containerImage
          env: [
            { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAIEndpoint }
            { name: 'AZURE_OPENAI_REALTIME_DEPLOYMENT', value: realtimeDeployment }
            { name: 'AZURE_OPENAI_RESPONSES_DEPLOYMENT', value: responsesDeployment }
            { name: 'DEVICE_TOKENS_JSON', secretRef: 'device-tokens' }
            { name: 'WEB_SEARCH_COUNTRY', value: 'JP' }
            { name: 'WEB_SEARCH_TIMEZONE', value: 'Asia/Tokyo' }
            { name: 'TRANSCRIPT_LOGGING', value: 'false' }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8080 }
              initialDelaySeconds: 5
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/readyz', port: 8080 }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

module foundryRbac './rbac.bicep' = if (!empty(foundryAccountName)) {
  name: 'foundry-rbac'
  params: {
    foundryAccountName: foundryAccountName
    principalId: app.identity.principalId
  }
}

output containerAppName string = app.name
output fqdn string = app.properties.configuration.ingress.fqdn
output principalId string = app.identity.principalId
