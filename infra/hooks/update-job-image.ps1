# Points the scheduled ACA job at the image that `azd deploy` just pushed for the app.
$ErrorActionPreference = 'Stop'
if (-not $env:SERVICE_APP_IMAGE_NAME) { throw 'SERVICE_APP_IMAGE_NAME is not set (run azd deploy first)' }
az containerapp job update `
  --name $env:AZURE_CONTAINER_APP_JOB_NAME `
  --resource-group $env:AZURE_RESOURCE_GROUP `
  --image $env:SERVICE_APP_IMAGE_NAME `
  --command life-helper-job `
  --output none
if ($LASTEXITCODE -ne 0) { throw 'az containerapp job update failed' }
Write-Host "Updated job $($env:AZURE_CONTAINER_APP_JOB_NAME) to $($env:SERVICE_APP_IMAGE_NAME)"
