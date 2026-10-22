targetScope = 'resourceGroup'

@description('Azure region.')
param location string = 'malaysiawest'

@description('Existing Azure Container Apps environment resource ID.')
param environmentId string

@description('Grafana managed identity resource ID.')
param identityId string

@description('Grafana managed identity client ID.')
param identityClientId string

@description('Grafana container image in Azure Container Registry.')
param image string

@description('Minimum Grafana replicas.')
@minValue(0)
@maxValue(1)
param minReplicas int = 0

@description('Maximum Grafana replicas.')
@minValue(1)
@maxValue(1)
param maxReplicas int = 1

@secure()
param grafanaAdminPassword string

var registryServer = split(image, '/')[0]

resource grafana 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'incident-grafana'
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
      secrets: [
        {
          name: 'grafana-admin-password'
          value: grafanaAdminPassword
        }
      ]

      ingress: {
        external: true
        targetPort: 3000
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
          name: 'grafana'
          image: image

          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }

          env: [
            { name: 'GF_SECURITY_ADMIN_USER', value: 'admin' }
            { name: 'GF_SECURITY_ADMIN_PASSWORD', secretRef: 'grafana-admin-password' }
            {
              name: 'GF_AZURE_MANAGED_IDENTITY_ENABLED'
              value: 'true'
            }
            {
              name: 'GF_AZURE_MANAGED_IDENTITY_CLIENT_ID'
              value: identityClientId
            }
            {
              name: 'GF_AUTH_ANONYMOUS_ENABLED'
              value: 'false'
            }
            {
              name: 'GF_USERS_ALLOW_SIGN_UP'
              value: 'false'
            }
          ]

          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/health'
                port: 3000
              }
              initialDelaySeconds: 20
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
                concurrentRequests: '10'
              }
            }
          }
        ]
      }
    }
  }
}

output grafanaAppName string = grafana.name
output grafanaUrl string = 'https://${grafana.properties.configuration.ingress.fqdn}'