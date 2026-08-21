targetScope = 'resourceGroup'

@description('Region for all resources. Keep every resource in one region: the load simulator must sit next to the platform or the latency numbers are meaningless.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Lowercase alphanumeric.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'incident'

var suffix = uniqueString(resourceGroup().id)
var acrName = '${namePrefix}acr${suffix}'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    // Deployments authenticate with a managed identity, so the shared admin
    // account stays switched off.
    adminUserEnabled: false
  }
}

// One identity per workload rather than a registry-wide admin credential, so
// pull rights can be revoked for a single service.
resource ingestionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-ingestion-id'
  location: location
}

resource ingestionAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, ingestionIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: ingestionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output environmentId string = containerEnv.id
output ingestionIdentityId string = ingestionIdentity.id
