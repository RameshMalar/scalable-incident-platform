targetScope = 'resourceGroup'

// Processing Worker. Consumes incidents from `incident-events`, validates them
// against the Common Event Model, and settles each one -- complete, abandon or
// dead-letter. It takes no inbound HTTP, so it has no ingress and no health
// probe: the platform's evidence that it is alive is the queue draining.
//
// That is also why it scales on queue depth rather than request concurrency.
// Without the KEDA rule below, an app with no ingress and `minReplicas: 0`
// scales to zero and has no way to start back up.

@description('Region for all resources.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Resource id of the Container Apps environment from core.bicep.')
param environmentId string

@description('Resource id of the worker identity (has Service Bus Data Receiver and AcrPull).')
param identityId string

@description('Client id of that identity, for DefaultAzureCredential.')
param identityClientId string

@description('Resource id of the identity the autoscaler polls the queue with (has Service Bus Data Owner on the queue).')
param scalerIdentityId string

@description('Fully qualified Service Bus host, e.g. incident-bus-xxx.servicebus.windows.net.')
param serviceBusFqdn string

@description('Short name of the Service Bus namespace. The KEDA scaler identifies the namespace by name, not by host.')
param serviceBusNamespace string

@description('Queue the worker consumes incidents from.')
param serviceBusQueue string = 'incident-events'

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

@description('Backlog each replica is expected to absorb. KEDA runs ceil(queueDepth / this) replicas, capped at maxReplicas.')
param messagesPerReplica int = 20

resource worker 'Microsoft.App/containerApps@2025-01-01' = {
  name: '${namePrefix}-worker'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      // The worker's own identity, used by the container to pull the image and
      // to receive messages, plus the autoscaler's, which the platform uses on
      // the app's behalf to read queue depth.
      '${identityId}': {}
      '${scalerIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      // Required for non-HTTP event scale rules: with multiple active
      // revisions the platform cannot decide which one the queue should
      // scale.
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
            {
              name: 'SERVICE_BUS_FQDN'
              value: serviceBusFqdn
            }
            {
              name: 'SERVICE_BUS_QUEUE'
              value: serviceBusQueue
            }
            // Selects the worker identity for DefaultAzureCredential. Two
            // identities are attached, so leaving this unset would leave the
            // SDK no way to choose between them.
            {
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
            }
            // Native AMQP on 5671, for the same reason the ingestion service
            // sets it: the WebSocket default exists for laptops behind a
            // firewall, and here the egress is ours.
            {
              name: 'SERVICE_BUS_TRANSPORT'
              value: 'amqp'
            }
            // Without this the worker logs incidents and stores none of them.
            // Same shape as EVENT_PUBLISHER on the ingestion side, and asserted
            // by CD for the same reason.
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
              // Managed identity rather than a connection string: the
              // namespace has `disableLocalAuth: true`, so there is no SAS key
              // for the scaler to hold even if we wanted one.
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
