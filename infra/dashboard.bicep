targetScope = 'resourceGroup'

// Business dashboard. A read-only Streamlit view over the incident store.
//
// Ingress is external with no authentication in front of it -- a deliberate,
// accepted choice rather than an oversight: anyone with the URL can see every
// tenant's incident data. If that ever needs locking down, Container Apps'
// built-in Entra authentication (an `authConfig` sub-resource) gates requests
// before they reach the app, with no code change here.

@description('Region for all resources.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Resource id of the Container Apps environment from core.bicep.')
param environmentId string

@description('Resource id of the dashboard identity (has Cosmos DB Built-in Data Reader and AcrPull).')
param identityId string

@description('Client id of that identity, for DefaultAzureCredential.')
param identityClientId string

@description('Cosmos DB account endpoint the dashboard reads incidents from.')
param cosmosEndpoint string

@description('Cosmos database holding the incident container.')
param cosmosDatabase string = 'incidents'

@description('Cosmos container incidents are read from, partitioned on /tenant_id.')
param cosmosContainer string = 'incidents'

@description('Fully qualified image reference, tagged with the commit it was built from.')
param image string

@description('Minimum replicas. 0 enables scale-to-zero; the first request after an idle period pays a cold start.')
param minReplicas int = 0

@description('Maximum replicas the autoscaler may create.')
param maxReplicas int = 3

var registryServer = split(image, '/')[0]

resource dashboard 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-dashboard'
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
        targetPort: 8501
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
          name: 'dashboard'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            // Selects the dashboard identity for DefaultAzureCredential.
            {
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
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
          probes: [
            // Streamlit's own health endpoint; does not touch Cosmos, so a
            // Cosmos-side problem surfaces in the app (an error banner) rather
            // than as a restart loop.
            {
              type: 'Liveness'
              httpGet: {
                path: '/_stcore/health'
                port: 8501
              }
              initialDelaySeconds: 10
              periodSeconds: 30
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

output appName string = dashboard.name
output url string = 'https://${dashboard.properties.configuration.ingress.fqdn}'
