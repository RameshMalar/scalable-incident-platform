targetScope = 'resourceGroup'

// Processing Worker.
//
// Consumes incidents from `incident-events`, validates them against the Common
// Event Model, persists them to Cosmos DB, and publishes them to
// `notification-events` for downstream notification delivery.
//
// The worker has no inbound HTTP traffic, so it has no ingress and no HTTP
// health probe. It scales based on Service Bus queue depth.

@description('Region for all resources.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Resource id of the Container Apps environment from core.bicep.')
param environmentId string

@description('Resource id of the worker identity (has Service Bus Data Receiver, notification queue Sender, and AcrPull).')
param identityId string

@description('Client id of the worker identity, for DefaultAzureCredential.')
param identityClientId string

@description('Resource id of the identity the autoscaler uses to read queue depth.')
param scalerIdentityId string

@description('Fully qualified Service Bus host, e.g. incident-bus-xxx.servicebus.windows.net.')
param serviceBusFqdn string

@description('Short name of the Service Bus namespace used by the KEDA scaler.')
param serviceBusNamespace string

@description('Queue the worker consumes incidents from.')
param serviceBusQueue string = 'incident-events'

@description('Queue the worker publishes processed incidents to for notification delivery.')
param notificationQueue string = 'notification-events'

@description('Cosmos DB account endpoint the worker writes incidents to.')
param cosmosEndpoint string

@description('Cosmos database holding the incident container.')
param cosmosDatabase string = 'incidents'

@description('Cosmos container incidents are written to, partitioned on /tenant_id.')
param cosmosContainer string = 'incidents'

@description('Registry login server, e.g. incidentacrxxx.azurecr.io.')
param registryServer string

@description('Fully qualified image reference, tagged with the commit it was built from.')
param image string

@description('Minimum replicas. 0 keeps the worker free while the queue is empty.')
param minReplicas int = 0

@description('Maximum replicas the autoscaler may create.')
param maxReplicas int = 10

@description('Backlog each replica is expected to absorb.')
param messagesPerReplica int = 20

resource worker 'Microsoft.App/containerApps@2025-01-01' = {
  name: '${namePrefix}-worker'
  location: location

  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      // Worker identity:
      // - pulls the image from ACR
      // - receives from incident-events
      // - sends to notification-events
      '${identityId}': {}

      // Separate identity used by the KEDA scaler to inspect queue depth.
      '${scalerIdentityId}': {}
    }
  }

  properties: {
    managedEnvironmentId: environmentId

    configuration: {
      // Required for queue-based scaling when minReplicas is zero.
      activeRevisionsMode: 'Single'

      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]
    }

    template: {
      containers: [
        {
          name: 'worker'
          image: image

          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }

          env: [
            // Service Bus namespace shared by both the consumer and publisher.
            {
              name: 'SERVICE_BUS_FQDN'
              value: serviceBusFqdn
            }

            // Queue consumed by the Processing Worker.
            {
              name: 'SERVICE_BUS_QUEUE'
              value: serviceBusQueue
            }

            // Enable downstream notification publishing.
            {
              name: 'NOTIFICATION_PUBLISHER'
              value: 'servicebus'
            }

            // Queue used for notification handoff.
            {
              name: 'NOTIFICATION_QUEUE'
              value: notificationQueue
            }

            // Required because multiple user-assigned identities are attached
            // to this Container App.
            {
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
            }

            {
              name: 'SERVICE_BUS_TRANSPORT'
              value: 'amqp'
            }

            // Enable Cosmos persistence.
            {
              name: 'INCIDENT_STORE'
              value: 'cosmos'
            }

            {
              name: 'COSMOS_ENDPOINT'
              value: cosmosEndpoint
            }

            {
              name: 'COSMOS_DATABASE'
              value: cosmosDatabase
            }

            {
              name: 'COSMOS_CONTAINER'
              value: cosmosContainer
            }
          ]
        }
      ]

      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas

        rules: [
          {
            name: 'queue-depth'

            custom: {
              type: 'azure-servicebus'

              // Managed identity is used instead of a Service Bus connection
              // string because local/SAS authentication is disabled.
              identity: scalerIdentityId

              metadata: {
                namespace: serviceBusNamespace
                queueName: serviceBusQueue
                messageCount: '${messagesPerReplica}'
              }
            }
          }
        ]
      }
    }
  }
}

output appName string = worker.name
output latestRevisionName string = worker.properties.latestRevisionName