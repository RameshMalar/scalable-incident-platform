targetScope = 'resourceGroup'

// Notification Service.
//
// Consumes processed incidents from `notification-events` and delivers
// notifications to downstream channels such as Telegram.
//
// The service has no inbound HTTP traffic. It therefore scales from Service
// Bus queue depth rather than HTTP request concurrency.
//
// Authentication:
// - Service Bus: managed identity
// - ACR: managed identity
// - Key Vault: managed identity
// - Telegram bot token: Key Vault secret reference

@description('Region for all resources.')
param location string = 'malaysiawest'

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Resource id of the Container Apps environment from core.bicep.')
param environmentId string

@description('Resource id of the Notification Service managed identity.')
param identityId string

@description('Client id of the Notification Service managed identity.')
param identityClientId string

@description('Resource id of the identity used by the autoscaler to read queue depth.')
param scalerIdentityId string

@description('Fully qualified Service Bus host.')
param serviceBusFqdn string

@description('Short name of the Service Bus namespace.')
param serviceBusNamespace string

@description('Queue containing incidents ready for notification.')
param serviceBusQueue string = 'notification-events'

@description('Registry login server.')
param registryServer string

@description('Fully qualified Notification Service image reference.')
param image string

@description('Telegram chat id that receives incident notifications.')
param telegramChatId string

@description('Full Azure Key Vault secret URI containing the Telegram bot token.')
param telegramBotTokenSecretUri string

@description('Minimum replicas. Zero allows the service to scale down while the queue is empty.')
param minReplicas int = 0

@description('Maximum Notification Service replicas. Telegram delivery is intentionally single-replica so per-chat rate limiting is global.')
param maxReplicas int = 1

@description('Backlog each replica is expected to process.')
param messagesPerReplica int = 20


// -----------------------------------------------------------------------------
// Notification Container App
// -----------------------------------------------------------------------------

resource notification 'Microsoft.App/containerApps@2025-01-01' = {
  name: '${namePrefix}-notification'
  location: location

  identity: {
    type: 'UserAssigned'

    userAssignedIdentities: {
      // Application identity:
      // - pulls image from ACR
      // - receives from notification-events
      // - reads Telegram token from Key Vault
      '${identityId}': {}

      // Separate identity used only by KEDA to inspect queue depth.
      '${scalerIdentityId}': {}
    }
  }

  properties: {
    managedEnvironmentId: environmentId

    configuration: {
      activeRevisionsMode: 'Single'

      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]

      // The Telegram token is never stored directly in this Bicep file.
      //
      // Container Apps retrieves it from Azure Key Vault by using the
      // Notification Service managed identity.
      secrets: [
        {
          name: 'telegram-bot-token'
          keyVaultUrl: telegramBotTokenSecretUri
          identity: identityId
        }
      ]
    }

    template: {
      containers: [
        {
          name: 'notification'
          image: image

          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }

          env: [
            // -----------------------------------------------------------------
            // Service Bus
            // -----------------------------------------------------------------

            {
              name: 'SERVICE_BUS_FQDN'
              value: serviceBusFqdn
            }

            {
              name: 'SERVICE_BUS_QUEUE'
              value: serviceBusQueue
            }

            // Required because multiple user-assigned identities are attached
            // to this Container App.
            {
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
            }

            {
              name: 'SERVICE_BUS_TRANSPORT'
              value: 'amqp'
            }


            // -----------------------------------------------------------------
            // Notification provider
            // -----------------------------------------------------------------

            // Local development defaults to MockNotifier when this variable
            // is absent. Azure explicitly enables Telegram.
            {
              name: 'NOTIFIER'
              value: 'telegram'
            }

            {
              name: 'TELEGRAM_CHAT_ID'
              value: telegramChatId
            }

            // The value is resolved from the Container Apps secret above,
            // which itself references Azure Key Vault.
            {
              name: 'TELEGRAM_BOT_TOKEN'
              secretRef: 'telegram-bot-token'
            }

            // Keep group delivery below Telegram's per-chat burst threshold.
            // With maxReplicas=1 this interval applies to the whole channel.
            {
              name: 'TELEGRAM_MIN_SEND_INTERVAL_SECONDS'
              value: '3.1'
            }

            {
              name: 'TELEGRAM_MAX_RATE_LIMIT_RETRIES'
              value: '1'
            }
          ]
        }
      ]


      // -----------------------------------------------------------------------
      // Queue-depth autoscaling
      // -----------------------------------------------------------------------

      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas

        rules: [
          {
            name: 'queue-depth'

            custom: {
              type: 'azure-servicebus'

              // KEDA uses its dedicated managed identity rather than a
              // connection string.
              identity: scalerIdentityId

              metadata: {
                namespace: serviceBusNamespace
                queueName: serviceBusQueue
                messageCount: '${messagesPerReplica}'
              }
            }
          }
        ]
      }
    }
  }
}


// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

output appName string = notification.name
output latestRevisionName string = notification.properties.latestRevisionName