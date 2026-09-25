#!/usr/bin/env pwsh
<#
.SYNOPSIS
  CI/CD（GitHub Actions → Azure）の設定を行う。手元での初回構築（docs/setup.md のステップ2）の後に実行する。何度実行しても同じ結果になる。

.DESCRIPTION
  1. Azure に GitHub Actions 用のアプリ登録（サービス プリンシパル）を作り、OIDC のフェデレーション資格情報を付ける
     （GitHub API からリポジトリの OIDC subject prefix を取得する。パスワードやキーは作らない）
  2. ロールを付与する: サブスクリプションに「共同作成者」、アプリのリソースグループに「ユーザー アクセス管理者」
  3. GitHub の環境 production を作り、デプロイに必要な変数とシークレットを登録する（値は azd 環境から読む）
  4. -EnableDeploy を付けた場合、リポジトリ変数 DEPLOY_ENABLED=true を設定して自動デプロイを有効にする

  前提: az login 済み（アプリ登録を作れる権限）、gh auth login 済み（usagiandkamex）、azd env が選択済み。

.EXAMPLE
  ./scripts/setup-cicd.ps1 -EnableDeploy
#>
param(
  [string]$Repo = 'usagiandkamex/life-helper',
  [string]$AppDisplayName = 'life-helper-github-deploy',
  [switch]$EnableDeploy
)
$ErrorActionPreference = 'Stop'

function Invoke-Checked {
  param([string]$Exe, [Parameter(ValueFromRemainingArguments)] [string[]]$Rest)
  $out = & $Exe @Rest 2>&1
  if ($LASTEXITCODE -ne 0) { throw "$Exe $($Rest -join ' ') failed: $out" }
  return $out
}

Write-Host '== 0/4 azd 環境の値を読み込み'
$envValues = Invoke-Checked azd env get-values --output json | Out-String | ConvertFrom-Json
foreach ($required in 'AZURE_ENV_NAME', 'AZURE_LOCATION', 'AZURE_SUBSCRIPTION_ID', 'AZURE_RESOURCE_GROUP') {
  if (-not $envValues.$required) { throw "$required が azd 環境にありません。先に docs/setup.md のステップ2を完了してください。" }
}
$subscriptionId = $envValues.AZURE_SUBSCRIPTION_ID
Invoke-Checked az account set --subscription $subscriptionId | Out-Null
$tenantId = (Invoke-Checked az account show --subscription $subscriptionId --query tenantId --output tsv).Trim()
Invoke-Checked az group show --name $envValues.AZURE_RESOURCE_GROUP --subscription $subscriptionId --query id --output tsv | Out-Null
Invoke-Checked gh repo view $Repo --json nameWithOwner | Out-Null

Write-Host "== 1/4 アプリ登録とフェデレーション資格情報（$AppDisplayName）"
$appId = (Invoke-Checked az ad app list --display-name $AppDisplayName --query '[0].appId' --output tsv | Out-String).Trim()
if (-not $appId) {
  $appId = (Invoke-Checked az ad app create --display-name $AppDisplayName --query appId --output tsv).Trim()
  Write-Host "   アプリ登録を作成しました: $appId"
}
$spId = (Invoke-Checked az ad sp list --filter "appId eq '$appId'" --query '[0].id' --output tsv | Out-String).Trim()
if (-not $spId) {
  $spId = (Invoke-Checked az ad sp create --id $appId --query id --output tsv).Trim()
}
$subjectPrefix = (Invoke-Checked gh api "repos/$Repo/actions/oidc/customization/sub" --jq .sub_claim_prefix | Out-String).Trim()
if (-not $subjectPrefix) {
  throw "GitHub OIDC subject prefix を取得できませんでした: $Repo"
}
$subject = "${subjectPrefix}:environment:production"
$existingCreds = Invoke-Checked az ad app federated-credential list --id $appId | Out-String | ConvertFrom-Json
$matchingCred = $existingCreds | Where-Object { $_.subject -eq $subject } | Select-Object -First 1
if (-not $matchingCred) {
  $productionCred = $existingCreds | Where-Object { $_.name -eq 'github-production' } | Select-Object -First 1
  $cred = @{
    issuer = 'https://token.actions.githubusercontent.com'
    subject = $subject
    audiences = @('api://AzureADTokenExchange')
  }
  if (-not $productionCred) {
    $cred.name = 'github-production'
  }
  $credFile = New-TemporaryFile
  try {
    Set-Content -Path $credFile -Value ($cred | ConvertTo-Json -Compress) -Encoding utf8
    if ($productionCred) {
      Invoke-Checked az ad app federated-credential update --id $appId --federated-credential-id $productionCred.id --parameters "@$credFile" | Out-Null
      Write-Host "   更新しました: $subject"
    } else {
      Invoke-Checked az ad app federated-credential create --id $appId --parameters "@$credFile" | Out-Null
      Write-Host "   作成しました: $subject"
    }
  } finally {
    Remove-Item $credFile -Force
  }
}

Write-Host '== 2/4 ロールの付与'
$assignments = @(
  @{ Role = 'Contributor'; Scope = "/subscriptions/$subscriptionId" },
  @{ Role = 'User Access Administrator'; Scope = "/subscriptions/$subscriptionId/resourceGroups/$($envValues.AZURE_RESOURCE_GROUP)" }
)
foreach ($a in $assignments) {
  $found = Invoke-Checked az role assignment list --assignee $spId --role $a.Role --scope $a.Scope --query '[0].id' --output tsv | Out-String
  if (-not $found.Trim()) {
    Invoke-Checked az role assignment create --assignee-object-id $spId --assignee-principal-type ServicePrincipal --role $a.Role --scope $a.Scope | Out-Null
    Write-Host "   $($a.Role) を付与しました: $($a.Scope)"
  }
}

Write-Host '== 3/4 GitHub の環境 production に変数とシークレットを登録'
Invoke-Checked gh api -X PUT "repos/$Repo/environments/production" | Out-Null
$variables = [ordered]@{
  AZURE_CLIENT_ID = $appId
  AZURE_TENANT_ID = $tenantId
  AZURE_SUBSCRIPTION_ID = $subscriptionId
  AZURE_ENV_NAME = $envValues.AZURE_ENV_NAME
  AZURE_LOCATION = $envValues.AZURE_LOCATION
  LH_GITHUB_OAUTH_CLIENT_ID = $envValues.LH_GITHUB_OAUTH_CLIENT_ID
  LH_GITHUB_APP_ID = $envValues.LH_GITHUB_APP_ID
  LH_GITHUB_APP_INSTALLATION_ID = $envValues.LH_GITHUB_APP_INSTALLATION_ID
  LH_NOTIFY_REPO = $envValues.LH_NOTIFY_REPO
  LH_BUDGET_AMOUNT = $envValues.LH_BUDGET_AMOUNT
  LH_BUDGET_EMAIL = $envValues.LH_BUDGET_EMAIL
  LH_BUDGET_START_DATE = $envValues.LH_BUDGET_START_DATE
}
foreach ($name in $variables.Keys) {
  $value = $variables[$name]
  if ([string]::IsNullOrEmpty($value)) { continue }
  Invoke-Checked gh variable set $name --env production --repo $Repo --body $value | Out-Null
  Write-Host "   変数 $name"
}
$secretNames = 'LH_GITHUB_OAUTH_CLIENT_SECRET', 'LH_SESSION_SECRET', 'LH_TOKEN_ENCRYPTION_KEY',
  'LH_RAKUTEN_APPLICATION_ID', 'LH_RAKUTEN_ACCESS_KEY', 'LH_GITHUB_APP_PRIVATE_KEY'
foreach ($name in $secretNames) {
  $value = $envValues.$name
  if ([string]::IsNullOrEmpty($value)) { continue }
  # Passed through stdin so the value never appears on a command line or in the console.
  $value | gh secret set $name --env production --repo $Repo | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "gh secret set $name failed" }
  Write-Host "   シークレット $name"
}

if ($EnableDeploy) {
  Write-Host '== 4/4 自動デプロイを有効化（リポジトリ変数 DEPLOY_ENABLED=true）'
  Invoke-Checked gh variable set DEPLOY_ENABLED --repo $Repo --body true | Out-Null
} else {
  Write-Host '== 4/4 自動デプロイは無効のまま（有効にするには -EnableDeploy を付けて再実行）'
}
Write-Host "完了: https://github.com/$Repo/settings/environments"
