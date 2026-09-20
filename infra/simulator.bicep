targetScope = 'resourceGroup'

// The load simulator, as a Container Apps job.
//
// A job rather than an app because a load run starts, drives a profile and
// ends. An app would be kept alive, scaled by its own autoscaler, and would
// have to be told to stop -- none of which a measurement wants.
//
// It lives in the same region as everything else on purpose. A generator in
// one region driving a platform in another adds inter-region latency to every
// sample, and the throughput and latency figures the platform is assessed on
// would then be measuring the distance between two Azure regions.
//
// Each simulated producer is a user-assigned managed identity, so the run
// holds no client secret: not in the repository, not in a GitHub secret, and
// not in the job's environment. Two things still have to be granted by hand,
// in the directory rather than in ARM, and the outputs below print exactly
// what to use:
//
//   1. `Incidents.Write` on the API registration, for each identity
//   2. an entry in the ingestion service's TENANT_MAP, per identity
//
// Until both are done the job authenticates and is refused with 403, which is
// the same answer any unregistered producer gets -- correctly so.

@description('Region for all resources. Must match the platform being measured.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Resource id of the Container Apps environment from core.bicep.')
param environmentId string

@description('Name of the container registry from core.bicep, to grant image pull on.')
param registryName string

@description('Fully qualified image reference, tagged with the commit it was built from.')
param image string

@description('Base URL of the deployed ingestion service the load is driven at.')
param targetUrl string

@description('The API\'s Application ID URI, api://<appId>. Tokens are requested for it.')
param authAudience string

@description('''
Simulated producers. Each gets its own managed identity and speaks for exactly
one tenant, which is what lets a run say something about tenant isolation
while several tenants are writing at once.
''')
param producers array = [
  {
    name: 'alpha'
    tenantId: 'TENANT-01'
  }
  {
    name: 'beta'
    tenantId: 'TENANT-02'
  }
  {
    name: 'gamma'
    tenantId: 'TENANT-03'
  }
]

@description('''
Load profile as rate:seconds stages.

Rates are set well above the store's sustainable write rate, which is about
101 incidents a second: one write costs a measured 9.9 RU against a 1,000 RU/s
free-tier grant. Everything above that builds a queue, which is the point --
the queue is what the platform absorbs a burst with, and what the worker
scales on.

Ingestion is a separate question, and the reason the rates go as high as they
do. It scales on fifty concurrent requests per replica, and at the twenty
milliseconds it answers in, even 140 incidents a second is three concurrent
requests: real traffic that never troubles the autoscaler. A rate that shows
ingestion scaling has to be an order of magnitude higher.

Stages are at least a minute because scaling is not instant. A first run
measured roughly a minute to go from one replica to four, so a stage shorter
than that measures the ramp rather than the platform at that rate.
''')
param stages string = '50:60,200:120,400:120,600:60'

@description('Fraction of incidents re-sent under an id already used, to exercise deduplication.')
param duplicateRate string = '0.02'

@description('''
Ceiling on requests in flight at once, set well above any rate the profile
asks for. Reaching it turns an open-loop run closed: the schedule stalls
behind the platform's latency, and the rate the run reports is then a property
of this number, not of the platform. A first run at 500 a second hit a ceiling
of 200 and reported 112 -- which is 200 divided by the latency, and says
nothing about what the platform could take.
''')
param maxInflight string = '2000'

@description('Seconds a replica may run before Container Apps kills it. Must exceed the profile.')
param replicaTimeout int = 1800

// AcrPull. Built-in, so the id is the same in every subscription.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource producerIdentities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [
  for producer in producers: {
    name: '${namePrefix}-loadsim-${producer.name}-id'
    location: location
  }
]

var identityIds = [for index in range(0, length(producers)): producerIdentities[index].id]

// Only the identity the job pulls its image with needs a role in Azure. The
// rest exist to hold a directory app role, which ARM cannot grant.
resource pullImage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identityIds[0], acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleId
    )
    principalId: producerIdentities[0].properties.principalId
    principalType: 'ServicePrincipal'
  }
}

module job 'simulator-job.bicep' = {
  name: 'load-simulator-job'
  params: {
    location: location
    name: '${namePrefix}-load-simulator'
    environmentId: environmentId
    identityIds: identityIds
    pullIdentityId: identityIds[0]
    producerSpecs: [
      for index in range(0, length(producers)): '${producers[index].name}:${producers[index].tenantId}:${producerIdentities[index].properties.clientId}'
    ]
    image: image
    targetUrl: targetUrl
    authAudience: authAudience
    stages: stages
    duplicateRate: duplicateRate
    maxInflight: maxInflight
    replicaTimeout: replicaTimeout
  }
  dependsOn: [
    pullImage
  ]
}

output jobName string = job.outputs.jobName

@description('Append these to the ingestion service\'s TENANT_MAP repository variable.')
output tenantMapAdditions array = [
  for index in range(0, length(producers)): '${producerIdentities[index].properties.clientId}:${producers[index].tenantId}'
]

@description('Grant Incidents.Write on the API registration to each of these service principals.')
output producerPrincipalIds array = [
  for index in range(0, length(producers)): producerIdentities[index].properties.principalId
]
