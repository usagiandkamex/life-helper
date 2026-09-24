#!/usr/bin/env pwsh
<#
.SYNOPSIS
  GitHub リポジトリの設定を行う（ブランチ戦略・main の保護・デプロイ用の環境）。何度実行しても同じ結果になる。

.DESCRIPTION
  - マージ方法を squash のみにし、マージ後にブランチを自動削除する
  - main をルールセット（.github/rulesets/protect-main.json）で保護する
      PR 必須 / CI の全ジョブ合格必須 / 直接 push・強制 push・削除の禁止 / 履歴を一直線に保つ
  - デプロイ用の環境 production を作り、main からのデプロイだけに限定する
  プライベートリポジトリでルールセットと環境の制限を使うには GitHub Pro 以上が必要。

.EXAMPLE
  ./scripts/setup-github-repo.ps1
#>
param(
  [string]$Repo = 'usagiandkamex/life-helper'
)
$ErrorActionPreference = 'Stop'
$rulesetFile = Join-Path $PSScriptRoot '..' '.github' 'rulesets' 'protect-main.json'

function Invoke-Gh {
  param([Parameter(ValueFromRemainingArguments)] [string[]]$GhArgs)
  $out = & gh @GhArgs 2>&1
  if ($LASTEXITCODE -ne 0) { throw "gh $($GhArgs -join ' ') failed: $out" }
  return $out
}

Write-Host '== 1/3 マージ設定（squash のみ・マージ後にブランチ削除・auto-merge 有効）'
Invoke-Gh api -X PATCH "repos/$Repo" `
  -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false `
  -F delete_branch_on_merge=true -F allow_auto_merge=true `
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY | Out-Null

Write-Host '== 2/3 main のルールセット（protect-main）'
$existing = (Invoke-Gh api "repos/$Repo/rulesets" | ConvertFrom-Json) | Where-Object { $_.name -eq 'protect-main' }
if ($existing) {
  Invoke-Gh api -X PUT "repos/$Repo/rulesets/$($existing.id)" --input $rulesetFile | Out-Null
  Write-Host "   更新しました（id=$($existing.id)）"
} else {
  $created = Invoke-Gh api -X POST "repos/$Repo/rulesets" --input $rulesetFile | ConvertFrom-Json
  Write-Host "   作成しました（id=$($created.id)）"
}

Write-Host '== 3/3 デプロイ用の環境 production（main からのみ）'
$envBody = @{ deployment_branch_policy = @{ protected_branches = $false; custom_branch_policies = $true } } | ConvertTo-Json
$envBody | gh api -X PUT "repos/$Repo/environments/production" --input - | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'environment production could not be configured' }
$policies = (Invoke-Gh api "repos/$Repo/environments/production/deployment-branch-policies" | ConvertFrom-Json).branch_policies
if (-not ($policies | Where-Object { $_.name -eq 'main' })) {
  Invoke-Gh api -X POST "repos/$Repo/environments/production/deployment-branch-policies" -f name=main -f type=branch | Out-Null
}

Write-Host "完了: https://github.com/$Repo/settings/rules"
