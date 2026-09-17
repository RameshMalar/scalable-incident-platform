targetScope = 'resourceGroup'

// Azure resource health, on its own, over the platform's own resources
// (Container Apps, Service Bus, Cosmos DB, ...) via Grafana's built-in Azure
// Monitor data source. Deliberately kept separate from business-dashboard --
// see AD note in the dashboard.bicep header for why that one stays incident
// data only.

@description('Region for all resources. Must match core.bicep.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Grafana SKU. Essential is the low-cost tier suited to a single team; Standard adds HA and more data sources.')
@allowed([
  'Essential'
  'Standard'
])
param skuName string = 'Essential'

// Monitoring Reader lets Grafana's built-in Azure Monitor data source read
// metrics and logs for every resource in this resource group, with no
// per-resource wiring. Scoped to the resource group rather than the
// subscription: this platform's resources all live in one RG, so that is
// as much visibility as the dashboard needs.
var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'

resource grafana 'Microsoft.Dashboard/grafana@2024-11-01-preview' = {
  name: '${namePrefix}-grafana'
  location: location
  sku: {
    name: skuName
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    zoneRedundancy: 'Disabled'
    publicNetworkAccess: 'Enabled'
    // Who gets Grafana Admin/Editor/Viewer on this instance is a one-time
    // human decision, not something to hardcode here -- granted afterwards
    // with `az role assignment create` against this resource.
    grafanaIntegrations: {
      azureMonitorWorkspaceIntegrations: []
    }
  }
}

resource grafanaMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, grafana.id, monitoringReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalId: grafana.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output grafanaName string = grafana.name
output grafanaEndpoint string = grafana.properties.endpoint
