targetScope = 'resourceGroup'

@description('Explicit opt-in. Default creates no queue or role assignments.')
param provisionQueue bool = false

@description('Existing managed-identity-only Standard or Premium namespace.')
param serviceBusNamespace string

@description('New queue only. The legacy notification-events queue is excluded.')
@allowed([
  'notification-events-dedup-v1'
  'notification-events-dedup-v2'
])
param notificationQueueName string = 'notification-events-dedup-v1'

@description('Existing Worker application identity object id.')
param workerPrincipalId string

@description('Existing Notification application identity object id.')
param notificationPrincipalId string

@description('Existing dedicated Notification scaler identity object id.')
param notificationScalerPrincipalId string

var senderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var receiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
// Same queue-depth permission as the existing scaler; never namespace scope.
var scalerRoleId = '090c5cfd-751d-490a-894a-3ce6f1109419'

resource namespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' existing = {
  name: serviceBusNamespace
}

resource queue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = if (provisionQueue) {
  parent: namespace
  name: notificationQueueName
  properties: {
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'P1D'
    enablePartitioning: false
    maxDeliveryCount: 5
    deadLetteringOnMessageExpiration: true
    lockDuration: 'PT5M'
    defaultMessageTimeToLive: 'P14D'
  }
}

resource workerSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (provisionQueue) {
  name: guid(queue.id, workerPrincipalId, senderRoleId)
  scope: queue
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', senderRoleId)
    principalId: workerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource notificationReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (provisionQueue) {
  name: guid(queue.id, notificationPrincipalId, receiverRoleId)
  scope: queue
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', receiverRoleId)
    principalId: notificationPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource notificationScaler 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (provisionQueue) {
  name: guid(queue.id, notificationScalerPrincipalId, scalerRoleId)
  scope: queue
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', scalerRoleId)
    principalId: notificationScalerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Pass this same name explicitly to both workload templates after migration review.
output notificationQueueName string = notificationQueueName
output provisioned bool = provisionQueue
