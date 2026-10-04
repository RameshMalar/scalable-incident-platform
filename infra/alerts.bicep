targetScope = 'resourceGroup'

// Baseline alerting on top of the resource-health workbook's data (see
// workbook.bicep). The workbook is passive -- someone has to open it to
// notice a problem. These are native Azure Monitor metric alerts, so they
// read Azure Monitor's live metrics store directly and do not depend on the
// Log Analytics export added in core.bicep for the workbook.
//
// The original scoping for the queue/worker condition was "worker replicas
// == 0 for >5 min while the queue has depth." A single metric alert can't
// AND two criteria that each scope a different resource type (a Container
// Apps metric and a Service Bus metric), so this alerts on sustained queue
// depth directly instead: Microsoft.ServiceBus/namespaces metric
// ActiveMessages, filtered to the incident queue, staying above zero for
// 10 minutes. That is a strictly more general stall signal -- it also
// catches a worker that is scaled up but crash-looping on every message.

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Name of the Service Bus namespace from core.bicep.')
param serviceBusNamespaceName string

@description('Queue the worker consumes. Backlog here for a sustained window means processing has stalled.')
param incidentQueueName string = 'incident-events'

@description('Name of the Cosmos DB account from core.bicep.')
param cosmosAccountName string

@description('Comma-separated email addresses that receive alert notifications.')
param alertEmails string

var trimmedAlertEmails = [for email in split(alertEmails, ','): trim(email)]

resource serviceBus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' existing = {
  name: serviceBusNamespaceName
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosAccountName
}

resource worker 'Microsoft.App/containerApps@2025-01-01' existing = {
  name: '${namePrefix}-worker'
}

resource ingestion 'Microsoft.App/containerApps@2024-03-01' existing = {
  name: '${namePrefix}-ingestion'
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${namePrefix}-ops-alerts'
  location: 'global'
  properties: {
    groupShortName: 'incidentops'
    enabled: true
    emailReceivers: [for (email, i) in trimmedAlertEmails: {
      name: 'ops-${i}'
      emailAddress: email
      useCommonAlertSchema: true
    }]
  }
}

resource incidentQueueBacklog 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${namePrefix}-incident-queue-backlog'
  location: 'global'
  properties: {
    description: 'Incident queue has had messages waiting for 10+ minutes -- processing has stalled, whether the worker failed to scale up or is crash-looping on every message.'
    severity: 1
    enabled: true
    scopes: [
      serviceBus.id
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    targetResourceType: 'Microsoft.ServiceBus/namespaces'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'queue-depth'
          metricName: 'ActiveMessages'
          metricNamespace: 'Microsoft.ServiceBus/namespaces'
          dimensions: [
            {
              name: 'EntityName'
              operator: 'Include'
              values: [
                incidentQueueName
              ]
            }
          ]
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Minimum'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

resource cosmosRuApproachingCap 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${namePrefix}-cosmos-ru-approaching-cap'
  location: 'global'
  properties: {
    description: 'Cosmos DB is consuming most of its provisioned RU/s -- requests will start getting throttled soon if this holds.'
    severity: 2
    enabled: true
    scopes: [
      cosmos.id
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    targetResourceType: 'Microsoft.DocumentDB/databaseAccounts'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'ru-consumption'
          metricName: 'NormalizedRUConsumption'
          metricNamespace: 'Microsoft.DocumentDB/databaseAccounts'
          operator: 'GreaterThan'
          threshold: 80
          timeAggregation: 'Maximum'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

resource cosmosThrottling 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${namePrefix}-cosmos-throttling'
  location: 'global'
  properties: {
    description: 'Cosmos DB is returning 429 (Request Rate Too Large) responses.'
    severity: 2
    enabled: true
    scopes: [
      cosmos.id
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    targetResourceType: 'Microsoft.DocumentDB/databaseAccounts'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'throttled-requests'
          metricName: 'TotalRequests'
          metricNamespace: 'Microsoft.DocumentDB/databaseAccounts'
          dimensions: [
            {
              name: 'StatusCode'
              operator: 'Include'
              values: [
                '429'
              ]
            }
          ]
          operator: 'GreaterThan'
          threshold: 10
          timeAggregation: 'Total'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

resource workerRestarting 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${namePrefix}-worker-restarting'
  location: 'global'
  properties: {
    description: 'The worker container app has restarted more than 3 times in 5 minutes.'
    severity: 2
    enabled: true
    scopes: [
      worker.id
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    targetResourceType: 'Microsoft.App/containerApps'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'restart-count'
          metricName: 'RestartCount'
          metricNamespace: 'Microsoft.App/containerApps'
          operator: 'GreaterThan'
          threshold: 3
          timeAggregation: 'Maximum'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

resource ingestionRestarting 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${namePrefix}-ingestion-restarting'
  location: 'global'
  properties: {
    description: 'The ingestion container app has restarted more than 3 times in 5 minutes.'
    severity: 2
    enabled: true
    scopes: [
      ingestion.id
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    targetResourceType: 'Microsoft.App/containerApps'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'restart-count'
          metricName: 'RestartCount'
          metricNamespace: 'Microsoft.App/containerApps'
          operator: 'GreaterThan'
          threshold: 3
          timeAggregation: 'Maximum'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

output actionGroupName string = actionGroup.name
