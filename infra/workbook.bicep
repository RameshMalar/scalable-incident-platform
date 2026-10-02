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

@description('Resource id of the Log Analytics workspace from core.bicep. The AzureMetrics queries below are bound to it explicitly -- the portal has nothing at resource-group scope to auto-detect one from.')
param logAnalyticsWorkspaceId string

var workbookDisplayName = '${namePrefix}-resource-health'

var workbookContent = {
  version: 'Notebook/1.0'
  items: [
    {
      type: 9
      name: 'parameters'
      content: {
        version: 'KqlParameterItem/1.0'
        parameters: [
          {
            id: '8cefd159-fb85-4e8c-ac98-20c1a0c18f2e'
            version: 'KqlParameterItem/1.0'
            name: 'ResourceGroup'
            type: 1
            description: 'Resource group for querying'
            isRequired: true
            value: resourceGroup().name
          }
          {
            id: '9cefd159-fb85-4e8c-ac98-20c1a0c18f3e'
            version: 'KqlParameterItem/1.0'
            name: 'TimeRange'
            type: 4
            value: {
              durationMs: 604800000
            }
            isHidden: true
          }
        ]
        style: 'pills'
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
      }
    }
    {
      type: 1
      name: 'text - intro'
      content: {
        json: '## ${namePrefix} resource health\nHealth and scaling metrics for Container Apps, Container Apps Environment, and Cosmos DB over the last 7 days. Data from Azure Monitor Metrics and Log Analytics.'
      }
    }
    {
      type: 3
      name: 'query - container apps inventory'
      content: {
        version: 'KqlItem/1.0'
        query: 'Resources\n| where resourceGroup =~ \'{ResourceGroup}\'\n| where type =~ \'microsoft.app/containerapps\'\n| project name, location, runningStatus = tostring(properties.runningStatus), latestRevisionName = tostring(properties.latestRevisionName)'
        queryType: 1
        resourceType: 'microsoft.resourcegraph/resources'
      }
    }
    {
      type: 3
      name: 'query - container apps replicas'
      content: {
        version: 'KqlItem/1.0'
        query: 'AzureMetrics\n| where TimeGenerated > ago(7d)\n| where ResourceGroup =~ \'{ResourceGroup}\'\n| where ResourceProvider == "MICROSOFT.APP"\n| where MetricName == "Replicas"\n| summarize CurrentValue = toreal(max(Maximum)) by Resource\n| project Resource, CurrentReplicas = CurrentValue\n| sort by Resource asc'
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        crossComponentResources: [
          logAnalyticsWorkspaceId
        ]
        gridSettings: {
          formatters: [
            {
              columnMatch: 'CurrentValue'
              formatter: 4
              formatOptions: {
                palette: 'blue'
              }
            }
          ]
        }
      }
    }
    {
      type: 3
      name: 'query - container apps cpu and memory'
      content: {
        version: 'KqlItem/1.0'
        query: 'AzureMetrics\n| where TimeGenerated > ago(7d)\n| where ResourceGroup =~ \'{ResourceGroup}\'\n| where ResourceProvider == "MICROSOFT.APP"\n| where MetricName in ("CpuPercentage", "MemoryPercentage")\n| summarize AvgPercent = avg(Average), MaxPercent = max(Maximum), P95Percent = percentile(Maximum, 95) by Resource, MetricName\n| project Resource, Metric = MetricName, AvgPercent = round(AvgPercent, 1), MaxPercent = round(MaxPercent, 1), P95Percent = round(P95Percent, 1)\n| sort by Resource asc'
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        crossComponentResources: [
          logAnalyticsWorkspaceId
        ]
      }
    }
    {
      type: 3
      name: 'query - container apps environment status'
      customWidth: '50'
      content: {
        version: 'KqlItem/1.0'
        query: 'Resources\n| where resourceGroup =~ \'{ResourceGroup}\'\n| where type =~ \'microsoft.app/managedenvironments\'\n| project name, location, provisioningState = tostring(properties.provisioningState)'
        queryType: 1
        resourceType: 'microsoft.resourcegraph/resources'
      }
    }
    {
      type: 3
      name: 'query - cosmos db inventory'
      customWidth: '50'
      content: {
        version: 'KqlItem/1.0'
        query: 'Resources\n| where resourceGroup =~ \'{ResourceGroup}\'\n| where type =~ \'microsoft.documentdb/databaseaccounts\'\n| project name, location, provisioningState = tostring(properties.provisioningState), throughputLimit = tostring(properties.capacity.totalThroughputLimit)'
        queryType: 1
        resourceType: 'microsoft.resourcegraph/resources'
      }
    }
    {
      type: 3
      name: 'query - cosmos db consumed ru'
      content: {
        version: 'KqlItem/1.0'
        query: 'AzureMetrics\n| where TimeGenerated > ago(7d)\n| where ResourceGroup =~ \'{ResourceGroup}\'\n| where ResourceProvider == "MICROSOFT.DOCUMENTDB"\n| where MetricName == "TotalRequestUnits" or MetricName == "ProvisionedThroughput"\n| summarize AvgConsumed = avg(iff(MetricName == "TotalRequestUnits", Total, real(0))), PeakConsumed = max(iff(MetricName == "TotalRequestUnits", Total, real(0))), AvgProvisioned = avg(iff(MetricName == "ProvisionedThroughput", Average, real(0))) by Resource\n| project Resource, AvgConsumedRU = round(AvgConsumed, 1), PeakConsumedRU = round(PeakConsumed, 1), ProvisionedRU = round(AvgProvisioned, 1)'
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        crossComponentResources: [
          logAnalyticsWorkspaceId
        ]
        gridSettings: {
          formatters: [
            {
              columnMatch: 'PeakConsumedRU'
              formatter: 4
              formatOptions: {
                palette: 'redGreen'
              }
            }
          ]
        }
      }
    }
    {
      type: 3
      name: 'query - cosmos db latency'
      content: {
        version: 'KqlItem/1.0'
        query: 'AzureMetrics\n| where TimeGenerated > ago(7d)\n| where ResourceGroup =~ \'{ResourceGroup}\'\n| where ResourceProvider == "MICROSOFT.DOCUMENTDB"\n| where MetricName in ("ServerSideLatencyDirect", "ServerSideLatencyGateway")\n| summarize AvgLatency = avg(Average), P95Latency = percentile(Maximum, 95), MaxLatency = max(Maximum) by Resource, MetricName\n| project Resource, Mode = MetricName, AvgLatency = round(AvgLatency, 2), P95Latency = round(P95Latency, 2), MaxLatency = round(MaxLatency, 2)'
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        crossComponentResources: [
          logAnalyticsWorkspaceId
        ]
      }
    }
    {
      type: 3
      name: 'query - cosmos db throttling'
      content: {
        version: 'KqlItem/1.0'
        query: 'AzureMetrics\n| where TimeGenerated > ago(7d)\n| where ResourceGroup =~ \'{ResourceGroup}\'\n| where ResourceProvider == "MICROSOFT.DOCUMENTDB"\n| where MetricName == "TotalRequests"\n| extend StatusCode = tostring(Tags["StatusCode"])\n| summarize TotalRequests = sum(Total), ThrottledRequests = sumif(Total, StatusCode == "429") by Resource\n| project Resource, TotalRequests, ThrottledRequests, ThrottledPercent = round(100.0 * ThrottledRequests / TotalRequests, 3)'
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        crossComponentResources: [
          logAnalyticsWorkspaceId
        ]
      }
    }
    {
      type: 3
      name: 'query - cosmos db storage'
      content: {
        version: 'KqlItem/1.0'
        query: 'AzureMetrics\n| where TimeGenerated > ago(7d)\n| where ResourceGroup =~ \'{ResourceGroup}\'\n| where ResourceProvider == "MICROSOFT.DOCUMENTDB"\n| where MetricName in ("DataUsage", "DocumentQuota")\n| summarize CurrentGB = max(Maximum) / 1024 / 1024 / 1024 by Resource, MetricName\n| project Resource, Metric = MetricName, CurrentGB = round(CurrentGB, 3)'
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        crossComponentResources: [
          logAnalyticsWorkspaceId
        ]
      }
    }
  ]
  fallbackResourceIds: [
    logAnalyticsWorkspaceId
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
