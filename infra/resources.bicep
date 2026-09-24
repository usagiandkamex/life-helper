param environmentName string
param location string
param tags object
param appImageName string
param allowedGithubUserId string
param githubOauthClientId string
@secure()
param githubOauthClientSecret string
@secure()
param sessionSecret string
@secure()
param tokenEncryptionKey string
@secure()
param stooqApiKey string
@secure()
param rakutenApplicationId string
@secure()
param rakutenAccessKey string
param githubAppId string
@secure()
param githubAppPrivateKey string
param githubAppInstallationId string
param notifyRepo string
param budgetAmount int
param budgetContactEmail string
@description('Fixed budget start date (first day of a month). Must not change after the budget is created.')
param budgetStartDate string

var token = uniqueString(subscription().id, resourceGroup().id, environmentName)
var appName = 'ca-lifehelper-${token}'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var appPlaceholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

var resolvedImage = appImageName
var hasImage = !empty(resolvedImage) && resolvedImage != appPlaceholderImage

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${token}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: 1 }
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${token}'
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'cr${token}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRoleId)
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${token}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    // Required: the ACA Azure Files mount authenticates with the storage account key.
    allowSharedKeyAccess: true
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    // Deleted shares can be restored for 14 days.
    shareDeleteRetentionPolicy: { enabled: true, days: 14 }
  }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'lifehelper'
  properties: {
    shareQuota: 10
    accessTier: 'TransactionOptimized'
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${token}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: share.name
      accessMode: 'ReadWrite'
    }
  }
}

var appUrl = 'https://${appName}.${env.properties.defaultDomain}'

// ACA secrets cannot be empty, so unused optional secrets hold "unset" and are simply not mapped to env vars.
var secrets = [
  { name: 'oauth-client-secret', value: empty(githubOauthClientSecret) ? 'unset' : githubOauthClientSecret }
  { name: 'session-secret', value: sessionSecret }
  { name: 'token-encryption-key', value: tokenEncryptionKey }
  { name: 'stooq-api-key', value: empty(stooqApiKey) ? 'unset' : stooqApiKey }
  { name: 'rakuten-application-id', value: empty(rakutenApplicationId) ? 'unset' : rakutenApplicationId }
  { name: 'rakuten-access-key', value: empty(rakutenAccessKey) ? 'unset' : rakutenAccessKey }
  { name: 'github-app-private-key', value: empty(githubAppPrivateKey) ? 'unset' : githubAppPrivateKey }
]

var optionalSecretEnv = concat(
  empty(githubOauthClientSecret) ? [] : [{ name: 'LH_GITHUB_OAUTH_CLIENT_SECRET', secretRef: 'oauth-client-secret' }],
  empty(stooqApiKey) ? [] : [{ name: 'LH_STOOQ_API_KEY', secretRef: 'stooq-api-key' }],
  empty(rakutenApplicationId) ? [] : [{ name: 'LH_RAKUTEN_APPLICATION_ID', secretRef: 'rakuten-application-id' }],
  empty(rakutenAccessKey) ? [] : [{ name: 'LH_RAKUTEN_ACCESS_KEY', secretRef: 'rakuten-access-key' }],
  empty(githubAppPrivateKey) ? [] : [{ name: 'LH_GITHUB_APP_PRIVATE_KEY', secretRef: 'github-app-private-key' }]
)

var commonEnv = concat(
  [
    { name: 'LH_ENVIRONMENT', value: 'production' }
    { name: 'LH_BASE_URL', value: appUrl }
    { name: 'LH_DATA_DIR', value: '/data' }
    { name: 'LH_ALLOWED_GITHUB_USER_ID', value: allowedGithubUserId }
    { name: 'LH_GITHUB_OAUTH_CLIENT_ID', value: githubOauthClientId }
    { name: 'LH_SESSION_SECRET', secretRef: 'session-secret' }
    { name: 'LH_TOKEN_ENCRYPTION_KEY', secretRef: 'token-encryption-key' }
    { name: 'LH_GITHUB_APP_ID', value: githubAppId }
    { name: 'LH_GITHUB_APP_INSTALLATION_ID', value: githubAppInstallationId }
    { name: 'LH_NOTIFY_REPO', value: notifyRepo }
  ],
  optionalSecretEnv
)

// SMB mounts are owned by root by default; the container runs as the non-root user "app" (uid/gid 10001).
var volumes = [
  {
    name: 'data'
    storageType: 'AzureFile'
    storageName: envStorage.name
    mountOptions: 'uid=10001,gid=10001,dir_mode=0750,file_mode=0640,nobrl'
  }
]
var volumeMounts = [{ volumeName: 'data', mountPath: '/data' }]
var registries = [{ server: registry.properties.loginServer, identity: identity.id }]

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: union(tags, { 'azd-service-name': 'app' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  dependsOn: [acrPull]
  properties: {
    managedEnvironmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        // Before the first deploy a public placeholder image (port 80) keeps the revision healthy.
        targetPort: hasImage ? 8000 : 80
        transport: 'auto'
        allowInsecure: false
      }
      registries: registries
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'app'
          image: hasImage ? resolvedImage : appPlaceholderImage
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: commonEnv
          volumeMounts: hasImage ? volumeMounts : []
          probes: hasImage
            ? [
                {
                  type: 'Liveness'
                  httpGet: { path: '/healthz', port: 8000 }
                  periodSeconds: 30
                  initialDelaySeconds: 15
                }
              ]
            : []
        }
      ]
      // Single replica: chat sessions, the SSE event buffer and Copilot session state live in one process.
      scale: { minReplicas: 0, maxReplicas: 1 }
      volumes: hasImage ? volumes : []
    }
  }
}

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: 'caj-lifehelper-${token}'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  dependsOn: [acrPull]
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      // Cron is UTC; automation times are converted from Japan time by the app. Due checks run every 15 minutes.
      scheduleTriggerConfig: { cronExpression: '*/15 * * * *', parallelism: 1, replicaCompletionCount: 1 }
      // 20-minute automation limit + time to save results and release locks.
      replicaTimeout: 1500
      replicaRetryLimit: 0
      registries: registries
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'job'
          image: hasImage ? resolvedImage : 'mcr.microsoft.com/azurelinux/base/core:3.0'
          command: hasImage ? ['life-helper-job'] : ['/bin/sh', '-c', 'echo waiting for the first deploy']
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: commonEnv
          volumeMounts: hasImage ? volumeMounts : []
        }
      ]
      volumes: hasImage ? volumes : []
    }
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = if (budgetAmount > 0 && !empty(budgetContactEmail) && !empty(budgetStartDate)) {
  name: 'budget-${token}'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        contactEmails: [budgetContactEmail]
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [budgetContactEmail]
      }
    }
  }
}

output registryLoginServer string = registry.properties.loginServer
output appName string = app.name
output jobName string = job.name
output appUrl string = appUrl
