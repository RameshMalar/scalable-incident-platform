targetScope = 'resourceGroup'

@description('Azure region for Grafana resources.')
param location string = 'malaysiawest'

@description('Resource naming prefix.')
param namePrefix string = 'incident'


// Built-in Azure Monitoring Reader role.
var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'

// Dedicated Grafana identity.
// No Cosmos DB, Service Bus data-plane, or Blob permissions.
resource grafanaIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-grafana-id'
  location: location
}

// Read-only Azure monitoring access at resource-group scope.
resource grafanaMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, grafanaIdentity.id, monitoringReaderRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      monitoringReaderRoleId
    )
    principalId: grafanaIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
// Existing Azure Container Registry.
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: 'incidentacr4pqaac2c4e4hs'
}

// Built-in AcrPull role.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// Allow Grafana to pull images from the existing registry.
resource grafanaAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, grafanaIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleId
    )
    principalId: grafanaIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


output grafanaIdentityId string = grafanaIdentity.id
output grafanaIdentityClientId string = grafanaIdentity.properties.clientId

