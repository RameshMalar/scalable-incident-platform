targetScope = 'resourceGroup'

@description('Region for all resources.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Lowercase alphanumeric.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'incident'

@description('Deploy identity from scripts/setup-github-oidc.sh. It is also what uploads evidence images, because a tenant Conditional Access policy can refuse a person a storage data-plane token while leaving managed identities unaffected.')
param deployIdentityName string = '${namePrefix}-github-deploy'


// -----------------------------------------------------------------------------
// Shared names and role definitions
// -----------------------------------------------------------------------------

var suffix = uniqueString(resourceGroup().id)

var acrName = '${namePrefix}acr${suffix}'

var serviceBusName = '${namePrefix}-bus-${suffix}'
var incidentQueueName = 'incident-events'
var notificationQueueName = 'notification-events'

var cosmosName = toLower('${namePrefix}-cosmos-${suffix}')
var cosmosDatabaseName = 'incidents'
var cosmosContainerName = 'incidents'

var keyVaultName = toLower('${take(namePrefix, 8)}kv${suffix}')

// Storage account names allow at most 24 lowercase alphanumeric characters,
// and uniqueString() already consumes 13 of them.
var evidenceStorageName = toLower('${take(namePrefix, 8)}evi${suffix}')
var evidenceContainerName = 'evidence'


// Azure Container Registry pull role.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// Service Bus data-plane roles.
var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
var serviceBusDataOwnerRoleId = '090c5cfd-751d-490a-894a-3ce6f1109419'

// Cosmos DB data-plane contributor role.
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'
var cosmosDataReaderRoleId = '00000000-0000-0000-0000-000000000001'

// Key Vault Secrets User.
// Notification Service may read secret values but cannot manage them.
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// Storage Blob Data Reader.
// Notification Service downloads evidence images but never writes them.
var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

// Storage Blob Data Contributor, for whoever uploads evidence.
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'


// -----------------------------------------------------------------------------
// Monitoring
// -----------------------------------------------------------------------------

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


// -----------------------------------------------------------------------------
// Azure Container Registry
// -----------------------------------------------------------------------------

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location

  sku: {
    name: 'Basic'
  }

  properties: {
    // Workloads pull images using managed identities.
    adminUserEnabled: false
  }
}


// -----------------------------------------------------------------------------
// Managed identities
// -----------------------------------------------------------------------------

resource ingestionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-ingestion-id'
  location: location
}

resource workerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-worker-id'
  location: location
}

resource notificationIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-notification-id'
  location: location
}

resource scalerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-scaler-id'
  location: location
}

resource notificationScalerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-notification-scaler-id'
  location: location
}


// -----------------------------------------------------------------------------
// ACR permissions
// -----------------------------------------------------------------------------

resource ingestionAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    registry.id,
    ingestionIdentity.id,
    acrPullRoleId
  )

  scope: registry

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleId
    )

    principalId: ingestionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource workerAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    registry.id,
    workerIdentity.id,
    acrPullRoleId
  )

  scope: registry

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleId
    )

    principalId: workerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource notificationAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    registry.id,
    notificationIdentity.id,
    acrPullRoleId
  )

  scope: registry

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleId
    )

    principalId: notificationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// Business dashboard uses its own read-only identity.
resource dashboardIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-dashboard-id'
  location: location
}

resource dashboardAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, dashboardIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleId
    )
    principalId: dashboardIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// -----------------------------------------------------------------------------
// Container Apps environment
// -----------------------------------------------------------------------------

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


// -----------------------------------------------------------------------------
// Service Bus
// -----------------------------------------------------------------------------

resource serviceBus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: serviceBusName
  location: location

  sku: {
    name: 'Standard'
    tier: 'Standard'
  }

  properties: {
    // Authentication is through managed identity and Azure RBAC.
    // SAS/local authentication remains disabled.
    disableLocalAuth: true
  }
}


// Main incident queue.
resource incidentQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: serviceBus
  name: incidentQueueName

  properties: {
    maxDeliveryCount: 5
    deadLetteringOnMessageExpiration: true
    lockDuration: 'PT1M'
    defaultMessageTimeToLive: 'P14D'
  }
}


// Notification delivery queue.
resource notificationQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: serviceBus
  name: notificationQueueName

  properties: {
    maxDeliveryCount: 5
    deadLetteringOnMessageExpiration: true
    // A Telegram 429 response can ask the consumer to wait around 30 seconds
    // before retrying. Five minutes leaves room for that wait and both HTTP
    // attempts without the broker expiring the message lock mid-delivery.
    lockDuration: 'PT5M'
    defaultMessageTimeToLive: 'P14D'
  }
}


// -----------------------------------------------------------------------------
// Service Bus RBAC
// -----------------------------------------------------------------------------

// Ingestion publishes incident events.
resource ingestionBusSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    serviceBus.id,
    ingestionIdentity.id,
    serviceBusDataSenderRoleId
  )

  scope: serviceBus

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      serviceBusDataSenderRoleId
    )

    principalId: ingestionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// Worker receives incident events.
resource workerBusReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    serviceBus.id,
    workerIdentity.id,
    serviceBusDataReceiverRoleId
  )

  scope: serviceBus

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      serviceBusDataReceiverRoleId
    )

    principalId: workerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// After persistence, Worker publishes to notification-events.
resource workerNotificationSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    notificationQueue.id,
    workerIdentity.id,
    serviceBusDataSenderRoleId
  )

  scope: notificationQueue

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      serviceBusDataSenderRoleId
    )

    principalId: workerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// Notification Service consumes notification-events.
resource notificationBusReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    notificationQueue.id,
    notificationIdentity.id,
    serviceBusDataReceiverRoleId
  )

  scope: notificationQueue

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      serviceBusDataReceiverRoleId
    )

    principalId: notificationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// Worker queue scaler.
resource scalerQueueOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    incidentQueue.id,
    scalerIdentity.id,
    serviceBusDataOwnerRoleId
  )

  scope: incidentQueue

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      serviceBusDataOwnerRoleId
    )

    principalId: scalerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// Notification queue scaler.
resource notificationScalerQueueOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    notificationQueue.id,
    notificationScalerIdentity.id,
    serviceBusDataOwnerRoleId
  )

  scope: notificationQueue

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      serviceBusDataOwnerRoleId
    )

    principalId: notificationScalerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// -----------------------------------------------------------------------------
// Cosmos DB
// -----------------------------------------------------------------------------

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: cosmosName
  location: location
  kind: 'GlobalDocumentDB'

  properties: {
    databaseAccountOfferType: 'Standard'

    enableFreeTier: true

    capacity: {
      totalThroughputLimit: 1000
    }

    // Managed identity only.
    disableLocalAuth: true

    consistencyPolicy: {
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

      partitionKey: {
        paths: [
          '/tenant_id'
        ]

        kind: 'Hash'
      }

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


// Worker writes incident documents.
resource workerCosmosContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos

  name: guid(
    cosmos.id,
    workerIdentity.id,
    cosmosDataContributorRoleId
  )

  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: workerIdentity.properties.principalId
    scope: cosmos.id
  }
}


// Dashboard is read-only in Cosmos DB.
resource dashboardCosmosReader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, dashboardIdentity.id, cosmosDataReaderRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataReaderRoleId}'
    principalId: dashboardIdentity.properties.principalId
    scope: cosmos.id
  }
}


// -----------------------------------------------------------------------------
// Key Vault
// -----------------------------------------------------------------------------

// Stores secrets required by the Notification Service.
//
// IMPORTANT:
// The actual Telegram bot token is NOT declared in this Bicep file.
// It will be inserted separately into Key Vault.
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location

  properties: {
    tenantId: subscription().tenantId

    sku: {
      family: 'A'
      name: 'standard'
    }

    // Use Azure RBAC instead of legacy access policies.
    enableRbacAuthorization: true

    // Protect against accidental secret deletion.
    enableSoftDelete: true
    softDeleteRetentionInDays: 7

    publicNetworkAccess: 'Enabled'
  }
}


// Notification Service may read Key Vault secret values.
resource notificationKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    keyVault.id,
    notificationIdentity.id,
    keyVaultSecretsUserRoleId
  )

  scope: keyVault

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      keyVaultSecretsUserRoleId
    )

    principalId: notificationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// -----------------------------------------------------------------------------
// Evidence blob storage
// -----------------------------------------------------------------------------

// Holds incident evidence images referenced by `evidence_uri`.
//
// Shared key access is disabled, so every caller authenticates through Entra
// and Azure RBAC. That keeps account keys and long-lived SAS URLs out of the
// system entirely, at the cost of requiring an explicit role assignment for
// anyone who needs to upload.
resource evidenceStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: evidenceStorageName
  location: location

  sku: {
    name: 'Standard_LRS'
  }

  kind: 'StorageV2'

  properties: {
    accessTier: 'Hot'

    allowSharedKeyAccess: false
    allowBlobPublicAccess: false

    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'

    publicNetworkAccess: 'Enabled'
  }
}


resource evidenceBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: evidenceStorage
  name: 'default'

  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}


resource evidenceContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: evidenceBlobService
  name: evidenceContainerName

  properties: {
    publicAccess: 'None'
  }
}


// Notification Service reads evidence to forward it through Telegram sendPhoto.
// Scoped to the container rather than the account so the role cannot reach any
// future container in the same account.
resource notificationEvidenceReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    evidenceContainer.id,
    notificationIdentity.id,
    storageBlobDataReaderRoleId
  )

  scope: evidenceContainer

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataReaderRoleId
    )

    principalId: notificationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// The deploy identity already exists: scripts/setup-github-oidc.sh creates it
// before anything here can be deployed, since it is what CD authenticates as.
resource deployIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: deployIdentityName
}


// Uploads go through the pipeline rather than someone's laptop. A tenant
// Conditional Access policy can refuse a person a token for
// `storage.azure.com` however much Azure RBAC it holds, and Conditional Access
// does not apply to managed identities.
resource deployEvidenceContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    evidenceContainer.id,
    deployIdentity.id,
    storageBlobDataContributorRoleId
  )

  scope: evidenceContainer

  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataContributorRoleId
    )

    principalId: deployIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}


// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

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

// Fully qualified Service Bus hostname, e.g.
// incident-bus-xxx.servicebus.windows.net
output serviceBusFqdn string = replace(
  replace(
    serviceBus.properties.serviceBusEndpoint,
    'https://',
    ''
  ),
  ':443/',
  ''
)

output incidentQueueName string = incidentQueue.name

output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosDatabaseName string = cosmosDatabase.name
output cosmosContainerName string = cosmosContainer.name

output notificationIdentityId string = notificationIdentity.id
output notificationIdentityClientId string = notificationIdentity.properties.clientId
output notificationScalerIdentityId string = notificationScalerIdentity.id
output notificationQueueName string = notificationQueue.name

output keyVaultName string = keyVault.name

output evidenceStorageName string = evidenceStorage.name
output evidenceContainerName string = evidenceContainer.name
output evidenceBlobEndpoint string = evidenceStorage.properties.primaryEndpoints.blob
