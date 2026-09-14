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

// Cosmos account names are globally addressed too, and must be lowercase.
var cosmosName = toLower('${namePrefix}-cosmos-${suffix}')
var cosmosDatabaseName = 'incidents'
var cosmosContainerName = 'incidents'

// Cosmos data-plane RBAC is its own system: these are not Azure role
// definitions and cannot be assigned with Microsoft.Authorization. The
// Contributor role reads and writes documents but cannot manage the account.
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'
// Reader cannot write, which is all the dashboard should ever be able to do.
var cosmosDataReaderRoleId = '00000000-0000-0000-0000-000000000001'

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

// The processing worker's identity: it pulls the worker image from the registry
// and receives from the queue. Kept separate from the ingestion identity so
// either service's rights can be revoked without touching the other.
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

// The ingestion service only ever publishes.
resource ingestionBusSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(serviceBus.id, ingestionIdentity.id, serviceBusDataSenderRoleId)
  scope: serviceBus
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataSenderRoleId)
    principalId: ingestionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The processing worker only ever consumes.
resource workerBusReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(serviceBus.id, workerIdentity.id, serviceBusDataReceiverRoleId)
  scope: serviceBus
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataReceiverRoleId)
    principalId: workerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The business dashboard's identity: it pulls its own image from the
// registry and reads incidents from Cosmos. Kept separate from the worker
// identity so revoking write access never has to also revoke read access.
resource dashboardIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-dashboard-id'
  location: location
}

resource dashboardAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, dashboardIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: dashboardIdentity.properties.principalId
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

// The incident store. See AD-07: the free tier carries 1,000 RU/s and 25 GB at
// no charge and does not expire, where the alternative -- PostgreSQL Flexible
// Server B1ms -- is about $21 a month and, being always on, would be the only
// component in this platform that cannot scale to zero.
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: cosmosName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    // One free-tier account is allowed per subscription. If this deployment
    // ever fails claiming the allocation is taken, another account in the
    // subscription is holding it.
    enableFreeTier: true
    // The hard ceiling AD-07 asks for. Provisioned throughput cannot be raised
    // past the free grant by accident, so an overrun throttles instead of
    // billing -- which matters on a subscription whose spending limit disables
    // everything when the credit runs out.
    capacity: {
      totalThroughputLimit: 1000
    }
    // Same posture as the Service Bus namespace: no keys to leak or rotate,
    // so the only way in is a managed identity holding a data-plane role.
    disableLocalAuth: true
    consistencyPolicy: {
      // Session is the default and is sufficient here: one writer, and readers
      // that only need to see their own writes.
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    backupPolicy: {
      type: 'Periodic'
      periodicModeProperties: {
        backupIntervalInMinutes: 1440
        backupRetentionIntervalInHours: 48
        backupStorageRedundancy: 'Local'
      }
    }
  }
}

// Throughput is shared at the database level rather than dedicated per
// container, so the subscription registry can be added later as a second
// container without provisioning a second 400 RU/s and leaving the free grant.
resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: cosmosDatabaseName
  properties: {
    resource: {
      id: cosmosDatabaseName
    }
    options: {
      throughput: 1000
    }
  }
}

resource cosmosContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: cosmosDatabase
  name: cosmosContainerName
  properties: {
    resource: {
      id: cosmosContainerName
      // Partitioned by tenant, which is how incidents are read: a customer
      // looks at their own sites. It also answers the "partitioning" item the
      // proposal lists under Incident Repository in section 6.4.
      partitionKey: {
        paths: [
          '/tenant_id'
        ]
        kind: 'Hash'
      }
      // The document id is the producer's event_id, so uniqueness is enforced
      // by the store itself. That is what lets the worker drop its in-memory
      // deduplication: a redelivered incident collides instead of being
      // written twice.
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          {
            path: '/*'
          }
        ]
        excludedPaths: [
          {
            path: '/evidence_uri/?'
          }
          {
            path: '/description/?'
          }
        ]
      }
    }
  }
}

// The worker writes incidents. Ingestion deliberately gets nothing: it
// publishes to the queue and never touches the store, which keeps the write
// path single-writer.
resource workerCosmosContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, workerIdentity.id, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: workerIdentity.properties.principalId
    scope: cosmos.id
  }
}

// The dashboard only ever reads. Granting it Contributor "just in case" would
// mean a bug in dashboard code could corrupt the store the worker owns.
resource dashboardCosmosReader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, dashboardIdentity.id, cosmosDataReaderRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataReaderRoleId}'
    principalId: dashboardIdentity.properties.principalId
    scope: cosmos.id
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
output dashboardIdentityId string = dashboardIdentity.id
output dashboardIdentityClientId string = dashboardIdentity.properties.clientId
output serviceBusName string = serviceBus.name
// Fully qualified host the SDK connects to, e.g. incident-bus-xxx.servicebus.windows.net
output serviceBusFqdn string = replace(replace(serviceBus.properties.serviceBusEndpoint, 'https://', ''), ':443/', '')
output incidentQueueName string = incidentQueue.name
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosDatabaseName string = cosmosDatabase.name
output cosmosContainerName string = cosmosContainer.name
