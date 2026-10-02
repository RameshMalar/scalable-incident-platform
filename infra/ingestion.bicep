targetScope = 'resourceGroup'

@description('Region for all resources.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Resource id of the Container Apps environment from core.bicep.')
param environmentId string

@description('Resource id of the identity that pulls from the registry.')
param identityId string

@description('Resource id of the Log Analytics workspace from core.bicep, for platform metrics.')
param logAnalyticsWorkspaceId string

@description('Client id of that identity. The SDK needs it to pick the right user-assigned identity when more than one is attached.')
param identityClientId string

@description('Fully qualified Service Bus host, e.g. incident-bus-xxx.servicebus.windows.net.')
param serviceBusFqdn string

@description('Queue the ingestion service publishes incidents to.')
param serviceBusQueue string = 'incident-events'

@description('Application ID URI of the platform API app registration, e.g. api://<appId>. Tokens must name it as their audience.')
param authAudience string

@description('Producer client id to tenant mapping, as "appId:TENANT-01,appId:TENANT-02". This is the tenant registry the Subscription API takes over.')
param tenantMap string

@description('Entra directory that issues producer tokens. Defaults to the subscription\'s own tenant.')
param authTenantId string = subscription().tenantId

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
            // Without this the service falls back to the in-memory mock
            // publisher: it would answer 202 to every incident and drop it.
            {
              name: 'EVENT_PUBLISHER'
              value: 'servicebus'
            }
            // Native AMQP on 5671. The WebSocket default exists for laptops
            // behind a firewall; here the egress is ours and the native port is
            // the faster of the two, which matters for the throughput figures.
            {
              name: 'SERVICE_BUS_TRANSPORT'
              value: 'amqp'
            }
            // Without this the API accepts an incident from anyone on the
            // internet, attributed to any tenant they name. /health reports the
            // mode and CD fails the deployment on anything but 'entra'.
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
            // Not a secret: these are public client ids and tenant labels. The
            // authorisation they carry comes from the signed token, not from
            // this list.
            {
              name: 'TENANT_MAP'
              value: tenantMap
            }
          ]
          probes: [
            // Liveness only asks whether the process is serving. It must not
            // depend on Service Bus: failing it restarts the container, and
            // restarting every replica during a broker blip turns a messaging
            // outage into a second outage.
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
            // Readiness asks the harder question -- can this replica reach
            // Service Bus -- so a replica that cannot publish is taken out of
            // rotation instead of accepting incidents and failing them. It is
            // pointed at /ready rather than /health precisely so that a broker
            // problem does not also trip liveness and restart the container.
            {
              type: 'Readiness'
              httpGet: {
                path: '/ready'
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

// Feeds the resource-health workbook's Replicas/CPU/memory queries against
// the AzureMetrics table -- nothing forwards these platform metrics to Log
// Analytics otherwise.
resource ingestionDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: ingestion
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output appName string = ingestion.name
output url string = 'https://${ingestion.properties.configuration.ingress.fqdn}'
