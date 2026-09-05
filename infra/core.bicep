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

// Data-plane roles for Service Bus. Producers get Sender, consumers get
// Receiver -- neither can manage the namespace, only push or pull messages.
var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
// Owner is the only built-in role that lets KEDA read a queue's depth.
// Receiver looks like it should be enough -- its management actions do include
// `Microsoft.ServiceBus/*/queues/read` -- but the scaler does not go through
// ARM. It asks the namespace itself for the queue's runtime properties, and
// that is a data-plane operation, where Receiver grants only
// `Microsoft.ServiceBus/*/receive/action`. Owner is the sole built-in role
// whose data actions cover it, and it also grants send, which is why it goes
// to a dedicated identity scoped to the one queue -- see `scalerIdentity`.
var serviceBusDataOwnerRoleId = '090c5cfd-751d-490a-894a-3ce6f1109419'

// Service Bus namespaces are globally addressed, so the name must be unique.
var serviceBusName = '${namePrefix}-bus-${suffix}'
var incidentQueueName = 'incident-events'

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

// Provisioned now so the messaging RBAC and the worker's pull rights exist
// before Malar's processing worker has an image. The worker deploys today on a
// placeholder; only its container image changes when that work lands.
resource workerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-worker-id'
  location: location
}

resource workerAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, workerIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: workerIdentity.properties.principalId
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

// Standard rather than Basic: Basic cannot carry topics, sessions or duplicate
// detection, so the first fan-out to a second consumer would force a namespace
// rebuild. Standard costs a few dollars a month and keeps that door open.
resource serviceBus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: serviceBusName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    // Services authenticate with managed identity and Azure RBAC. Local SAS
    // keys stay switched off so there is no shared secret to leak or rotate.
    disableLocalAuth: true
  }
}

resource incidentQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: serviceBus
  name: incidentQueueName
  properties: {
    // A message the worker abandons five times is a poison message: it moves to
    // the dead-letter subqueue instead of blocking the queue behind it forever.
    maxDeliveryCount: 5
    // Expiry is a failure too, so time-out messages dead-letter rather than
    // vanish -- nothing leaves the system without a trace.
    deadLetteringOnMessageExpiration: true
    lockDuration: 'PT1M'
    defaultMessageTimeToLive: 'P14D'
  }
}

// Ingestion (Yixin's POST) only ever publishes.
resource ingestionBusSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(serviceBus.id, ingestionIdentity.id, serviceBusDataSenderRoleId)
  scope: serviceBus
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataSenderRoleId)
    principalId: ingestionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The worker (Malar's processor) only ever consumes.
resource workerBusReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(serviceBus.id, workerIdentity.id, serviceBusDataReceiverRoleId)
  scope: serviceBus
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataReceiverRoleId)
    principalId: workerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The autoscaler is a third party to the messaging: KEDA, running inside the
// Container Apps platform, polls the queue's runtime properties every 30
// seconds to decide how many worker replicas to run. That read is a data-plane
// operation the worker's Receiver role deliberately does not cover.
//
// Giving that right to the worker identity would quietly hand the worker's own
// code the ability to publish incidents, so it goes to an identity of its own,
// scoped to `incident-events` rather than to the namespace: this principal can
// manage exactly one queue and cannot see any topic or queue added later.
resource scalerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-scaler-id'
  location: location
}

resource scalerQueueOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(incidentQueue.id, scalerIdentity.id, serviceBusDataOwnerRoleId)
  scope: incidentQueue
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataOwnerRoleId)
    principalId: scalerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output environmentId string = containerEnv.id
output ingestionIdentityId string = ingestionIdentity.id
output ingestionIdentityClientId string = ingestionIdentity.properties.clientId
output workerIdentityId string = workerIdentity.id
output workerIdentityClientId string = workerIdentity.properties.clientId
output scalerIdentityId string = scalerIdentity.id
output serviceBusName string = serviceBus.name
// Fully qualified host the SDK connects to, e.g. incident-bus-xxx.servicebus.windows.net
output serviceBusFqdn string = replace(replace(serviceBus.properties.serviceBusEndpoint, 'https://', ''), ':443/', '')
output incidentQueueName string = incidentQueue.name
