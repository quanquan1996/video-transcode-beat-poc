#!/usr/bin/env bash
# Push a sample video through the pipeline N times in parallel, then print the latency/cost report.
#   ./run.sh <region> <s3://src-bucket/video.mp4> [copies]
set -euo pipefail
REGION=$1; SRC=$2; N=${3:-1}
STACK=${STACK:-beat-poc}
cd "$(dirname "$0")"
BUCKET=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text)
SRC_BUCKET=$(echo "$SRC" | cut -d/ -f3)
SRC_REGION=$(aws s3api get-bucket-location --bucket "$SRC_BUCKET" --query LocationConstraint --output text)
[ "$SRC_REGION" = None ] && SRC_REGION=us-east-1

STAGE="s3://$BUCKET/staging/$(basename "$SRC")"
if ! aws s3 ls --region "$REGION" "$STAGE" >/dev/null 2>&1; then
  echo "== staging sample into $STAGE (server-side copy)"
  aws s3 cp --region "$REGION" --source-region "$SRC_REGION" "$SRC" "$STAGE" --only-show-errors
fi

SINCE=$(date -u +%s)
RUN=$(date -u +%Y%m%d-%H%M%S)
echo "== submitting $N video(s) to s3://$BUCKET/input/"
for i in $(seq 1 "$N"); do
  aws s3 cp --region "$REGION" "$STAGE" "s3://$BUCKET/input/$RUN-$i.mp4" --only-show-errors &
done
wait
python3 report.py --region "$REGION" --stack "$STACK" --since "$SINCE" --expect "$N"
