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
Load profile as rate:seconds stages. The default steps up to the store's
sustainable write rate and then crosses it: one incident write costs 9.9 RU
against a 1,000 RU/s free-tier grant, so about 101 writes per second is the
ceiling. Throttling that was planned for is a result; throttling that was
stumbled into is noise.
''')
param stages string = '20:120,60:180,100:240,140:120'

@description('Fraction of incidents re-sent under an id already used, to exercise deduplication.')
param duplicateRate string = '0.02'

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
