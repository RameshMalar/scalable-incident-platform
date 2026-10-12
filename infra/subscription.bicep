targetScope = 'resourceGroup'

@description('Region for all resources. Must match core.bicep.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'incident'

@description('Container Apps environment resource id from core.bicep.')
@minLength(1)
param environmentId string

@description('Dedicated Subscription API identity resource id from core.bicep.')
@minLength(1)
param identityId string

@description('Dedicated identity client id used by DefaultAzureCredential.')
@minLength(1)
param identityClientId string

@description('Existing Cosmos account endpoint from core.bicep.')
@minLength(1)
param cosmosEndpoint string

@description('Existing shared-throughput database from core.bicep.')
@minLength(1)
param cosmosDatabase string = 'incidents'

@description('Subscription container from core.bicep; identity RBAC must match.')
@minLength(1)
param cosmosSubscriptionsContainer string = 'subscriptions'

@description('Approved Entra API token audience. No default; supply reviewed configuration.')
@minLength(1)
param authAudience string

@description('Approved clientId:platformTenant entries, comma-separated. Not a secret. Use only clients authorized to manage subscriptions.')
@minLength(1)
param tenantMap string

@description('Entra token issuer directory. Defaults to the Azure subscription tenant.')
@minLength(1)
param authTenantId string = subscription().tenantId

@description('Fully qualified image in the existing ACR, pinned to an immutable commit tag or digest.')
@minLength(1)
param image string

// Deliberately bounded for the current shared-throughput and quota budget.
@description('Minimum replicas. Zero permits scale-to-zero and cold starts.')
@allowed([
  0
  1
])
param minReplicas int = 0

@description('Maximum replicas. Review quota and shared Cosmos load before increasing.')
@allowed([
  1
])
param maxReplicas int = 1

var registryServer = split(image, '/')[0]

// Public HTTPS endpoint, following ingestion.bicep. Protected CRUD routes
// validate Entra bearer tokens, permissions and TENANT_MAP in the application.
// /health, /ready and the framework's API documentation remain public.
resource subscriptionApi 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-subscription'
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
      activeRevisionsMode: 'Single'
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
          name: 'subscription'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
            }
            {
              name: 'SUBSCRIPTION_STORE'
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
              name: 'COSMOS_SUBSCRIPTIONS_CONTAINER'
              value: cosmosSubscriptionsContainer
            }
            {
              name: 'AUTH_MODE'
              value: 'entra'
            }
            {
              name: 'AUTH_TENANT_ID'
              value: authTenantId
            }
            {
              name: 'AUTH_AUDIENCE'
              value: authAudience
            }
            {
              name: 'TENANT_MAP'
              value: tenantMap
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
                path: '/ready'
                port: 8000
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              // The store readiness operation has a five-second timeout.
              timeoutSeconds: 10
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
                concurrentRequests: '10'
              }
            }
          }
        ]
      }
    }
  }
}

output appName string = subscriptionApi.name
output url string = 'https://${subscriptionApi.properties.configuration.ingress.fqdn}'
output latestRevisionName string = subscriptionApi.properties.latestRevisionName
