targetScope = 'resourceGroup'

// Business dashboard. A read-only Streamlit view over the incident store.
//
// release = 'standard' (the default) renders exactly the template that has
// always been deployed: external ingress with no sign-in, so anyone with the
// URL can see every tenant's incident data.
//
// Microsoft sign-in (Entra, verified in the app) is introduced without risking
// an Admin lockout, in two separately approved steps:
//
//   'signin-test'  New revision at 0% traffic, reachable only on its own
//                  label URL (<app>---signin-test.<domain>). The revision that
//                  serves production now keeps 100%. Sign in there first.
//   'promote'      The new revision takes 100%; the previous one stays active
//                  at 0% under the label 'previous' for instant rollback.
//
// Both mount the Streamlit [auth] TOML from a Key Vault secret the dashboard
// identity reads, outside /app/.streamlit so the SOC theme config stays put.
// The image's auth_preflight.py refuses to start if that file does not match
// this revision, so a bad configuration never takes traffic.
//
//   'demo-login'   Demonstration fallback while Entra consent is pending: a
//                  new revision at 0% traffic on its own label URL
//                  (<app>---demo-login.<domain>), signing in with one
//                  username and a scrypt password hash mounted from Key
//                  Vault (business-dashboard/local_auth.py). The production
//                  revision keeps 100% and the Entra signin-test revision keeps
//                  its label and secret. Read-only: no Entra-only feature runs.

@description('standard (today), signin-test (0% traffic, label URL), promote (100%, previous kept) or demo-login (local sign-in, 0% traffic, label URL).')
@allowed([
  'standard'
  'signin-test'
  'promote'
  'demo-login'
])
param release string = 'standard'

@description('demo-login: Key Vault secret URI (versionless) holding [local_auth] username + password_hash.')
param localAuthSecretUri string = ''

@description('demo-login: the Entra signin-test revision to keep reachable at 0% under its label (empty: none).')
param entraTestRevision string = ''

@description('demo-login: label for the NEW revision and its URL (<app>---<label>.<domain>).')
param stagingLabel string = 'demo-login'

@description('demo-login: earlier labelled revisions to keep at 0% by name, e.g. [{ revisionName: "incident-dashboard--0000034", label: "demo-login" }]. Without this, a label bound to "latest" would move to the new revision.')
param keptRevisions array = []

@description('signin-test/promote: dashboard login app registration (client) ID.')
param authClientId string = ''

@description('signin-test/promote: Key Vault secret URI (versionless) holding this revision\'s Streamlit [auth] TOML.')
param authSecretUri string = ''

@description('signin-test/promote: revision currently serving production (kept at 100% for a test, at 0% as "previous" on promote).')
param stableRevision string = ''

@description('Optional: delegated API scope the dashboard requests, checked at startup when central access is enabled.')
param apiScope string = ''

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

@description('Resource id of the Log Analytics workspace from core.bicep, for platform metrics.')
param logAnalyticsWorkspaceId string

@description('Cosmos DB account endpoint the dashboard reads incidents from.')
param cosmosEndpoint string

@description('Cosmos database holding the incident container.')
param cosmosDatabase string = 'incidents'

@description('Cosmos container incidents are read from, partitioned on /tenant_id.')
param cosmosContainer string = 'incidents'

@description('Blob endpoint holding incident evidence. Empty hides evidence, and incidents are shown without it.')
param evidenceBlobEndpoint string = ''

@description('Blob container holding incident evidence.')
param evidenceContainer string = 'evidence'

@description('Fully qualified image reference, tagged with the commit it was built from.')
param image string

@description('Minimum replicas. 0 enables scale-to-zero; the first request after an idle period pays a cold start.')
param minReplicas int = 0

@description('Maximum replicas the autoscaler may create.')
param maxReplicas int = 3

var registryServer = split(image, '/')[0]
var appName = '${namePrefix}-dashboard'
var signIn = release != 'standard'
var localLogin = release == 'demo-login'
var entraLogin = signIn && !localLogin

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: last(split(environmentId, '/'))
}

// The callback this revision serves: the label URL while testing, the
// production URL once promoted. It must also be registered on the login app.
var publicHost = release == 'signin-test' || localLogin
  ? '${appName}---${localLogin ? stagingLabel : release}.${environment.properties.defaultDomain}'
  : '${appName}.${environment.properties.defaultDomain}'
var redirectUri = 'https://${publicHost}/oauth2callback'

var localLoginEnv = [
  {
    name: 'DASHBOARD_AUTH_MODE'
    value: 'local'
  }
  {
    name: 'DASHBOARD_LOCAL_AUTH_FILE'
    value: '/mnt/dashboard-local-auth/local-auth.toml'
  }
]

var signInEnv = localLogin ? localLoginEnv : entraLogin
  ? [
      {
        name: 'DASHBOARD_AUTH_MODE'
        value: 'oidc'
      }
      {
        name: 'DASHBOARD_AUTH_CLIENT_ID'
        value: authClientId
      }
      {
        name: 'DASHBOARD_EXPECTED_REDIRECT_URI'
        value: redirectUri
      }
      {
        name: 'STREAMLIT_SECRETS_FILES'
        value: '/mnt/dashboard-auth/secrets.toml'
      }
      {
        name: 'DASHBOARD_API_SCOPE'
        value: apiScope
      }
    ]
  : []

var keptTraffic = [for kept in keptRevisions: {
  revisionName: kept.revisionName
  weight: 0
  label: kept.label
}]

var demoLoginTraffic = concat(
  [
    {
      revisionName: stableRevision
      weight: 100
      label: 'stable'
    }
  ],
  empty(entraTestRevision)
    ? []
    : [
        {
          revisionName: entraTestRevision
          weight: 0
          label: 'signin-test'
        }
      ],
  keptTraffic,
  [
    {
      latestRevision: true
      weight: 0
      label: stagingLabel
    }
  ]
)

// Container Apps replaces the whole secret list on update, so a demo-login
// release keeps the Entra secret that the signin-test revision still mounts.
var appSecrets = concat(
  empty(authSecretUri)
    ? []
    : [
        {
          name: 'dashboard-auth'
          keyVaultUrl: authSecretUri
          identity: identityId
        }
      ],
  localLogin
    ? [
        {
          name: 'dashboard-local-auth'
          keyVaultUrl: localAuthSecretUri
          identity: identityId
        }
      ]
    : []
)

var traffic = localLogin ? demoLoginTraffic : release == 'signin-test'
  ? [
      {
        revisionName: stableRevision
        weight: 100
        label: 'stable'
      }
      {
        latestRevision: true
        weight: 0
        label: 'signin-test'
      }
    ]
  : concat(
      [
        {
          latestRevision: true
          weight: 100
          label: 'production'
        }
      ],
      empty(stableRevision)
        ? []
        : [
            {
              revisionName: stableRevision
              weight: 0
              label: 'previous'
            }
          ]
    )

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
    configuration: union(
      {
        ingress: union(
          {
            external: true
            targetPort: 8501
            transport: 'auto'
            allowInsecure: false
          },
          signIn ? { traffic: traffic } : {}
        )
        registries: [
          {
            server: registryServer
            identity: identityId
          }
        ]
      },
      signIn
        ? {
            activeRevisionsMode: 'Multiple'
            secrets: appSecrets
          }
        : {}
    )
    template: {
      containers: [
        {
          name: 'dashboard'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          volumeMounts: localLogin
            ? [
                {
                  volumeName: 'dashboard-local-auth'
                  mountPath: '/mnt/dashboard-local-auth'
                }
              ]
            : entraLogin
                ? [
                    {
                      volumeName: 'dashboard-auth'
                      mountPath: '/mnt/dashboard-auth'
                    }
                  ]
                : null
          env: concat([
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
            {
              name: 'EVIDENCE_BLOB_ENDPOINT'
              value: evidenceBlobEndpoint
            }
            {
              name: 'EVIDENCE_CONTAINER'
              value: evidenceContainer
            }
          ], signInEnv)
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
      volumes: localLogin
        ? [
            {
              name: 'dashboard-local-auth'
              storageType: 'Secret'
              secrets: [
                {
                  secretRef: 'dashboard-local-auth'
                  path: 'local-auth.toml'
                }
              ]
            }
          ]
        : entraLogin
            ? [
                {
                  name: 'dashboard-auth'
                  storageType: 'Secret'
                  secrets: [
                    {
                      secretRef: 'dashboard-auth'
                      path: 'secrets.toml'
                    }
                  ]
                }
              ]
            : null
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
resource dashboardDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: dashboard
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

output appName string = dashboard.name
output url string = 'https://${dashboard.properties.configuration.ingress.fqdn}'
@description('The URL this release serves sign-in on: the label URL for signin-test and demo-login, otherwise production.')
output signInUrl string = 'https://${publicHost}'
