targetScope = 'resourceGroup'

// The load simulator's job resource. Separated from simulator.bicep because
// the producer identities' client ids are only known once those identities
// exist, and Bicep will not let a variable loop over a value that late. A
// module parameter can carry it, so the identities are declared there and the
// job that consumes them is declared here.

@description('Region for the job. Must match the platform being measured.')
param location string = 'malaysiawest'

@description('Name of the job.')
param name string

@description('Resource id of the Container Apps environment from core.bicep.')
param environmentId string

@description('Resource ids of every producer identity to attach to the job.')
param identityIds array

@description('Resource id of the identity that pulls the image. One of identityIds.')
param pullIdentityId string

@description('name:tenant:clientId for each simulated producer.')
param producerSpecs array

@description('Fully qualified image reference, tagged with the commit it was built from.')
param image string

@description('Base URL of the deployed ingestion service the load is driven at.')
param targetUrl string

@description('The API\'s Application ID URI, api://<appId>. Tokens are requested for it.')
param authAudience string

@description('Load profile as rate:seconds stages.')
param stages string

@description('Fraction of incidents re-sent under an id already used.')
param duplicateRate string

@description('Seconds a replica may run before Container Apps kills it.')
param replicaTimeout int

var registryServer = split(image, '/')[0]

resource simulator 'Microsoft.App/jobs@2024-03-01' = {
  name: name
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: toObject(identityIds, id => id, id => {})
  }
  properties: {
    environmentId: environmentId
    configuration: {
      // Manual: deploying the job must not run a load test. CD deploys this
      // definition on every merge, and a profile that drove itself would
      // hammer the platform on each one and bill for it.
      triggerType: 'Manual'
      replicaTimeout: replicaTimeout
      // A retried load run is a second, overlapping load run. It would double
      // the offered rate and make both measurements meaningless, so a failed
      // replica stays failed and is looked at.
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registryServer
          identity: pullIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'load-simulator'
          image: image
          // The generator has to hold its own schedule while hundreds of
          // requests are in flight. It reports when it falls behind rather
          // than hiding it, but a CPU-starved replica guarantees it will --
          // and the run would then describe this container instead of the
          // platform.
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            {
              name: 'TARGET_URL'
              value: targetUrl
            }
            {
              name: 'LOAD_STAGES'
              value: stages
            }
            {
              name: 'LOAD_PRODUCERS'
              value: join(producerSpecs, ',')
            }
            {
              name: 'AUTH_AUDIENCE'
              value: authAudience
            }
            // Demanded rather than inferred. The simulator would otherwise
            // fall back to looking for client secrets when the identity
            // endpoint is missing, and would report the missing secret
            // instead of the missing identity, which is the actual fault.
            {
              name: 'MANAGED_IDENTITY'
              value: '1'
            }
            {
              name: 'DUPLICATE_RATE'
              value: duplicateRate
            }
          ]
        }
      ]
    }
  }
}

output jobName string = simulator.name
