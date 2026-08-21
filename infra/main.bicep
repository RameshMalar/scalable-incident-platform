targetScope = 'resourceGroup'

@description('Region for all resources. Keep every resource in one region: the load simulator must sit next to the platform or the latency numbers are meaningless.')
param location string = 'southeastasia'

@description('Short prefix for generated resource names. Lowercase alphanumeric.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'incident'

@description('Container image for the ingestion service. Defaults to a placeholder so the first deployment succeeds before any image has been built.')
param ingestionImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Minimum replicas. 0 enables scale-to-zero.')
param ingestionMinReplicas int = 0

@description('Maximum replicas the autoscaler may create.')
param ingestionMaxReplicas int = 10

var suffix = uniqueString(resourceGroup().id)
var acrName = '${namePrefix}acr${suffix}'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

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

resource ingestion 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-ingestion'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${ingestionIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: ingestionIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'ingestion'
          image: ingestionImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 5
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 3
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: ingestionMinReplicas
        maxReplicas: ingestionMaxReplicas
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
  dependsOn: [
    ingestionAcrPull
  ]
}

output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output ingestionAppName string = ingestion.name
output ingestionUrl string = 'https://${ingestion.properties.configuration.ingress.fqdn}'
