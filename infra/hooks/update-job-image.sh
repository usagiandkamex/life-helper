#!/bin/sh
# Points the scheduled ACA job at the image that `azd deploy` just pushed for the app.
set -eu
: "${SERVICE_APP_IMAGE_NAME:?SERVICE_APP_IMAGE_NAME is not set (run azd deploy first)}"
az containerapp job update \
  --name "$AZURE_CONTAINER_APP_JOB_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --image "$SERVICE_APP_IMAGE_NAME" \
  --command python -m life_helper.jobs run-due \
  --output none
echo "Updated job $AZURE_CONTAINER_APP_JOB_NAME to $SERVICE_APP_IMAGE_NAME"
