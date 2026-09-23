targetScope = 'resourceGroup'

// Azure resource health, on its own, over the platform's own resources
// (Container Apps, Service Bus, Cosmos DB, ...). Deliberately kept separate
// from business-dashboard -- see AD note in the dashboard.bicep header for
// why that one stays incident data only.
//
// This used to be an Azure Managed Grafana workspace (see git history), but
// the Essential SKU is deprecated for new workspaces and Standard bills
// per-hour regardless of use -- not worth it on a student subscription for a
// view that only needs to answer "is everything up". A workbook is free: it
// is just a saved query/chart layout over Azure Monitor and Resource Graph
// data the platform is already emitting.

@description('Region for all resources. Must match core.bicep.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

var workbookDisplayName = '${namePrefix}-resource-health'

var workbookContent = {
  version: 'Notebook/1.0'
  items: [
    {
      type: 1
      name: 'text - intro'
      content: {
        json: '## ${namePrefix} resource health\nAzure Resource Graph inventory and status for this resource group\'s Container Apps, Service Bus, and Cosmos DB resources. Incident data lives in the business dashboard, not here.'
      }
    }
    {
      type: 3
      name: 'query - container apps'
      content: {
        version: 'KqlItem/1.0'
        query: 'Resources\n| where resourceGroup =~ \'${resourceGroup().name}\'\n| where type =~ \'microsoft.app/containerapps\'\n| project name, location, runningStatus = tostring(properties.runningStatus), latestRevisionName = tostring(properties.latestRevisionName)'
        queryType: 1
        resourceType: 'microsoft.resourcegraph/resources'
      }
    }
    {
      type: 3
      name: 'query - service bus queues'
      content: {
        version: 'KqlItem/1.0'
        query: 'Resources\n| where resourceGroup =~ \'${resourceGroup().name}\'\n| where type =~ \'microsoft.servicebus/namespaces/queues\'\n| project name, status = tostring(properties.status), activeMessageCount = tostring(properties.countDetails.activeMessageCount), deadLetterMessageCount = tostring(properties.countDetails.deadLetterMessageCount)'
        queryType: 1
        resourceType: 'microsoft.resourcegraph/resources'
      }
    }
    {
      type: 3
      name: 'query - cosmos db'
      content: {
        version: 'KqlItem/1.0'
        query: 'Resources\n| where resourceGroup =~ \'${resourceGroup().name}\'\n| where type =~ \'microsoft.documentdb/databaseaccounts\'\n| project name, location, provisioningState = tostring(properties.provisioningState), documentEndpoint = tostring(properties.documentEndpoint)'
        queryType: 1
        resourceType: 'microsoft.resourcegraph/resources'
      }
    }
  ]
  fallbackResourceIds: [
    resourceGroup().id
  ]
  '$schema': 'https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json'
}

resource workbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(resourceGroup().id, 'resource-health-workbook')
  location: location
  kind: 'shared'
  properties: {
    displayName: workbookDisplayName
    serializedData: string(workbookContent)
    category: 'workbook'
    sourceId: resourceGroup().id
  }
}

output workbookName string = workbook.properties.displayName
output workbookId string = workbook.id
