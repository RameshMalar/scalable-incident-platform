targetScope = 'resourceGroup'

// Least-privilege data-plane grants for activating sign-in, Telegram
// self-service and the Notification send-time recheck.
//
// NOT deployed by CD or CD on merge: no workflow references this file, so a
// merge can never grant anything. Each grant is off by default and is applied
// only by an explicitly approved, manual deployment that names it, for example
//
//   az deployment group what-if   -g rg-incident-platform -f infra/activation-grants.bicep -p grantNotificationSubscriptionsReader=true
//   az deployment group create    (the same, after approval)
//
// Every assignment names one existing identity and the narrowest scope that
// works: one Cosmos container, or one Key Vault secret. Nothing here creates a
// container, secret, identity or user role. Secret values are written into Key
// Vault by a person, outside this template, and never pass through it.
// See business-dashboard/ACTIVATION_CHECKLIST.md.

@description('Short prefix for generated resource names. Must match core.bicep.')
param namePrefix string = 'incident'

@description('Existing Cosmos DB account (incident-cosmos-<suffix>).')
param cosmosAccountName string

@description('Existing Key Vault (incidentkv<suffix>).')
param keyVaultName string

@description('Notification Service: Cosmos DB Built-in Data Reader on the subscriptions container only, for NOTIFICATION_ELIGIBILITY=enforce.')
param grantNotificationSubscriptionsReader bool = false

@description('Subscription API: Key Vault Secrets User on the telegram-webhook-secret secret only.')
param grantSubscriptionWebhookSecret bool = false

@description('Dashboard sign-in test: Key Vault Secrets User on the dashboard-auth-signin-test secret only.')
param grantDashboardSigninTestSecret bool = false

@description('Dashboard demo-login: Key Vault Secrets User on the dashboard-local-auth secret only.')
param grantDashboardLocalAuthSecret bool = false

var cosmosDataReaderRoleId = '00000000-0000-0000-0000-000000000001'
// Key Vault Secrets User: read secret values, nothing else.
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosAccountName
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource notificationIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: '${namePrefix}-notification-id'
}

resource subscriptionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: '${namePrefix}-subscription-id'
}

resource dashboardIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: '${namePrefix}-dashboard-id'
}

// Data-plane scope /dbs/<db>/colls/<container>, as core.bicep uses. Point
// reads of a subscription and of its owner's access/binding records.
var subscriptionsScope = '${cosmos.id}/dbs/incidents/colls/subscriptions'

resource notificationSubscriptionsReader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (grantNotificationSubscriptionsReader) {
  parent: cosmos
  name: guid(subscriptionsScope, notificationIdentity.id, cosmosDataReaderRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataReaderRoleId}'
    principalId: notificationIdentity.properties.principalId
    scope: subscriptionsScope
  }
}

resource webhookSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: 'telegram-webhook-secret'
}

resource subscriptionWebhookSecretUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantSubscriptionWebhookSecret) {
  name: guid(webhookSecret.id, subscriptionIdentity.id, keyVaultSecretsUserRoleId)
  scope: webhookSecret
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: subscriptionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Sign-in test only: the dashboard identity may read the one secret holding
// the test revision's Streamlit [auth] TOML -- not the vault, not any other
// secret. The production secret's grant is deliberately absent; it is a
// separate, later approval made only when promoting (ACTIVATION_CHECKLIST.md).
resource dashboardSigninTestSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: 'dashboard-auth-signin-test'
}

resource dashboardSigninTestSecretUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantDashboardSigninTestSecret) {
  name: guid(dashboardSigninTestSecret.id, dashboardIdentity.id, keyVaultSecretsUserRoleId)
  scope: dashboardSigninTestSecret
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: dashboardIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Demo-login fallback: the dashboard identity may read the one secret holding
// [local_auth] username + scrypt password hash -- not the vault, not any other
// secret. Applied only after that secret exists.
resource dashboardLocalAuthSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: 'dashboard-local-auth'
}

resource dashboardLocalAuthSecretUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantDashboardLocalAuthSecret) {
  name: guid(dashboardLocalAuthSecret.id, dashboardIdentity.id, keyVaultSecretsUserRoleId)
  scope: dashboardLocalAuthSecret
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: dashboardIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
