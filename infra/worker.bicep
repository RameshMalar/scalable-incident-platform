targetScope = 'resourceGroup'

// Processing Worker skeleton. Provisioned ahead of Malar's code so the wiring
// -- environment, identity, Service Bus receive rights and the env vars the
// consumer will read -- is proven end to end while the container is still a
// placeholder. When the real image lands, only the `image` parameter and the
// scale rule change; everything around it already works.

@description('Region for all resources.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Resource id of the Container Apps environment from core.bicep.')
param environmentId string

@description('Resource id of the worker identity (has Service Bus Data Receiver).')
param identityId string

@description('Client id of that identity, for DefaultAzureCredential.')
param identityClientId string

@description('Fully qualified Service Bus host, e.g. incident-bus-xxx.servicebus.windows.net.')
param serviceBusFqdn string

@description('Queue the worker consumes incidents from.')
param serviceBusQueue string = 'incident-events'

@description('Registry login server, e.g. incidentacrxxx.azurecr.io. Only used when the placeholder is replaced by a private image.')
param registryServer string = ''

@description('Container image. Defaults to a public placeholder that exits immediately; replaced with the real worker image once it exists.')
param image string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Minimum replicas. 0 keeps the skeleton free while idle.')
param minReplicas int = 0

@description('Maximum replicas the autoscaler may create.')
param maxReplicas int = 10

// The placeholder is public, so no registry credential is needed for it. Only
// wire up the private registry once a real, pullable image is supplied.
var usesPrivateRegistry = !empty(registryServer)

resource worker 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-worker'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      // A background consumer takes no inbound HTTP, so it has no ingress and
      // therefore no health probe to fail -- the existing /health check in CD
      // stays untouched.
      registries: usesPrivateRegistry ? [
        {
          server: registryServer
          identity: identityId
        }
      ] : []
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
            {
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        // Placeholder rule. Replace with a KEDA azure-servicebus scaler keyed on
        // queue depth once the worker actually consumes.
      }
    }
  }
}

output appName string = worker.name
