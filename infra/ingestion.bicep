targetScope = 'resourceGroup'

@description('Region for all resources.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Resource id of the Container Apps environment from core.bicep.')
param environmentId string

@description('Resource id of the identity that pulls from the registry.')
param identityId string

@description('Client id of that identity. The SDK needs it to pick the right user-assigned identity when more than one is attached.')
param identityClientId string

@description('Fully qualified Service Bus host, e.g. incident-bus-xxx.servicebus.windows.net.')
param serviceBusFqdn string

@description('Queue the ingestion service publishes incidents to.')
param serviceBusQueue string = 'incident-events'

@description('Fully qualified image reference, tagged with the commit it was built from.')
param image string

@description('Minimum replicas. 0 enables scale-to-zero.')
param minReplicas int = 0

@description('Maximum replicas the autoscaler may create.')
param maxReplicas int = 10

var registryServer = split(image, '/')[0]

resource ingestion 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-ingestion'
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
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
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
          name: 'ingestion'
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
            // Selects the user-assigned identity above for DefaultAzureCredential.
            {
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

output appName string = ingestion.name
output url string = 'https://${ingestion.properties.configuration.ingress.fqdn}'
