targetScope = 'subscription'

@minLength(1)
@maxLength(40)
@description('azd environment name (used for resource names).')
param environmentName string

@minLength(1)
@description('Azure region for all resources.')
param location string

@description('Container image for the app and the job (set by azd deploy). Empty on the first provision.')
param appImageName string = ''

@description('Numeric GitHub user id allowed to sign in (usagiandkamex = 134019422).')
param allowedGithubUserId string = '134019422'

@description('GitHub OAuth App client id. Empty on the first provision (the app URL is needed to create the OAuth App).')
param githubOauthClientId string = ''

@secure()
param githubOauthClientSecret string = ''

@secure()
@description('Random string used to sign session cookies.')
param sessionSecret string

@secure()
@description('Fernet key used to encrypt the stored GitHub token.')
param tokenEncryptionKey string

@secure()
param stooqApiKey string = ''

@secure()
param rakutenApplicationId string = ''

@secure()
param rakutenAccessKey string = ''

param githubAppId string = ''

@secure()
param githubAppPrivateKey string = ''

param githubAppInstallationId string = ''
param notifyRepo string = ''

@description('Monthly budget amount in the billing currency (string because azd substitutes env values as text). 0 disables the budget alert.')
param budgetAmount string = '0'

@description('E-mail address for budget alerts.')
param budgetContactEmail string = ''

@description('Budget start date (first day of a month, e.g. 2026-10-01). Set once and keep it: Azure cannot change a budget start date.')
param budgetStartDate string = ''

var tags = { 'azd-env-name': environmentName, app: 'life-helper' }

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
    tags: tags
    appImageName: appImageName
    allowedGithubUserId: allowedGithubUserId
    githubOauthClientId: githubOauthClientId
    githubOauthClientSecret: githubOauthClientSecret
    sessionSecret: sessionSecret
    tokenEncryptionKey: tokenEncryptionKey
    stooqApiKey: stooqApiKey
    rakutenApplicationId: rakutenApplicationId
    rakutenAccessKey: rakutenAccessKey
    githubAppId: githubAppId
    githubAppPrivateKey: githubAppPrivateKey
    githubAppInstallationId: githubAppInstallationId
    notifyRepo: notifyRepo
    budgetAmount: int(empty(budgetAmount) ? '0' : budgetAmount)
    budgetContactEmail: budgetContactEmail
    budgetStartDate: budgetStartDate
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer
output AZURE_CONTAINER_APP_NAME string = resources.outputs.appName
output AZURE_CONTAINER_APP_JOB_NAME string = resources.outputs.jobName
output SERVICE_APP_ENDPOINT_URL string = resources.outputs.appUrl
