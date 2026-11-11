#!/usr/bin/env bash
# Delete everything the POC created in one region:  ./cleanup.sh <region>
set -euo pipefail
REGION=$1; STACK=${STACK:-beat-poc}
BUCKET=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text)
aws s3 rm --region "$REGION" "s3://$BUCKET" --recursive --only-show-errors
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
echo "deleted $STACK in $REGION"
