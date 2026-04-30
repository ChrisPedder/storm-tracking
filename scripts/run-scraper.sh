#!/usr/bin/env bash
set -euo pipefail

PROFILE="${AWS_PROFILE:-storm-tracking}"
REGION="eu-central-1"
STACK_NAME="StormTrackingPipeline"

usage() {
  cat <<USAGE
Run an individual scraper task on Fargate.

Usage: $0 <scraper> [start-year] [end-year]

Scrapers:
  eswd          ESWD severe weather events
  blitzortung   Blitzortung lightning strokes
  era5          ERA5 reanalysis (single + pressure levels)
  topo          Copernicus DEM topography (no year range needed)

Examples:
  $0 eswd 2023 2023           # Scrape ESWD for 2023 only
  $0 blitzortung 2023 2023    # Blitzortung for 2023
  $0 era5 2023 2023           # ERA5 for one year (still large!)
  $0 topo                     # Download DEM tiles

Environment:
  AWS_PROFILE   AWS CLI profile (default: storm-tracking)
USAGE
  exit 1
}

[[ $# -lt 1 ]] && usage

SCRAPER="$1"
START_YEAR="${2:-}"
END_YEAR="${3:-}"

case "$SCRAPER" in
  eswd)        FAMILY="storm-tracking-eswd-scraper" ;;
  blitzortung) FAMILY="storm-tracking-blitzortung-scraper" ;;
  era5)        FAMILY="storm-tracking-era5-downloader" ;;
  topo)        FAMILY="storm-tracking-topo-downloader" ;;
  *)           echo "Error: unknown scraper '$SCRAPER'" && echo && usage ;;
esac

if [[ "$SCRAPER" != "topo" && -z "$START_YEAR" ]]; then
  echo "Error: start-year is required for $SCRAPER"
  echo
  usage
fi

echo "==> Fetching stack outputs from $STACK_NAME..."
OUTPUTS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --profile "$PROFILE" \
  --region "$REGION" \
  --query "Stacks[0].Outputs" \
  --output json)

get_output() {
  echo "$OUTPUTS" | python3 -c "
import json, sys
outputs = json.load(sys.stdin)
for o in outputs:
    if o['OutputKey'] == '$1':
        print(o['OutputValue'])
        break
"
}

CLUSTER_ARN=$(get_output ClusterArn)
SUBNETS=$(get_output PublicSubnetIds)
SG=$(get_output TaskSecurityGroupId)
BUCKET=$(get_output BucketName)

echo "    Cluster: $CLUSTER_ARN"
echo "    Bucket:  $BUCKET"
echo "    Subnets: $SUBNETS"

# Get the latest task definition revision
TASK_DEF=$(aws ecs list-task-definitions \
  --family-prefix "$FAMILY" \
  --sort DESC \
  --max-items 1 \
  --profile "$PROFILE" \
  --region "$REGION" \
  --query "taskDefinitionArns[0]" \
  --output text)

echo "    Task:    $TASK_DEF"

# Get the container name from the task definition
CONTAINER_NAME=$(aws ecs describe-task-definition \
  --task-definition "$TASK_DEF" \
  --profile "$PROFILE" \
  --region "$REGION" \
  --query "taskDefinition.containerDefinitions[0].name" \
  --output text)

# Build environment overrides
ENV_OVERRIDES="[]"
if [[ -n "$START_YEAR" ]]; then
  ENV_OVERRIDES=$(cat <<JSON
[
  {"name": "START_YEAR", "value": "$START_YEAR"},
  {"name": "END_YEAR", "value": "${END_YEAR:-$START_YEAR}"}
]
JSON
)
fi

OVERRIDES=$(cat <<JSON
{
  "containerOverrides": [{
    "name": "$CONTAINER_NAME",
    "environment": $ENV_OVERRIDES
  }]
}
JSON
)

echo
echo "==> Starting $SCRAPER task..."
TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER_ARN" \
  --task-definition "$TASK_DEF" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides "$OVERRIDES" \
  --profile "$PROFILE" \
  --region "$REGION" \
  --query "tasks[0].taskArn" \
  --output text)

TASK_ID="${TASK_ARN##*/}"
echo "    Task ARN: $TASK_ARN"
echo
echo "==> View logs:"
echo "    aws logs tail /storm-tracking/pipeline --follow --profile $PROFILE --region $REGION"
echo
echo "==> Check task status:"
echo "    aws ecs describe-tasks --cluster $CLUSTER_ARN --tasks $TASK_ID --profile $PROFILE --region $REGION --query 'tasks[0].lastStatus' --output text"
echo
echo "==> Check S3 output:"
echo "    aws s3 ls s3://$BUCKET/raw/$SCRAPER/ --recursive --profile $PROFILE --region $REGION"
