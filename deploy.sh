#!/usr/bin/env bash
# Deploy the POC stack and build the worker image. Runs as-is in AWS CloudShell.
#   ./deploy.sh [region]          e.g. ./deploy.sh us-west-1
# Env: STACK (default beat-poc), SEGMENTS (ffmpeg slices, default 8),
#      ACCEL (MediaConvert acceleration: PREFERRED default | DISABLED), SKIP_BUILD=1 to reuse the image
set -euo pipefail
REGION=${1:-${AWS_REGION:-us-west-1}}
STACK=${STACK:-beat-poc}
cd "$(dirname "$0")"

echo "== deploying stack $STACK in $REGION"
aws cloudformation deploy --region "$REGION" --stack-name "$STACK" --template-file template.yaml \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides SegmentCount="${SEGMENTS:-8}" McAcceleration="${ACCEL:-PREFERRED}"

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
BUCKET=$(out BucketName); PROJECT=$(out BuildProject)

if [ -n "${SKIP_BUILD:-}" ]; then echo "== SKIP_BUILD set, keeping current image"; exit 0; fi
echo "== building worker image with CodeBuild ($PROJECT)"
python3 -c "import shutil; shutil.make_archive('/tmp/beat-poc-src', 'zip', '.', 'container')"
aws s3 cp --region "$REGION" /tmp/beat-poc-src.zip "s3://$BUCKET/build/src.zip" --only-show-errors
BID=$(aws codebuild start-build --region "$REGION" --project-name "$PROJECT" --query build.id --output text)
while :; do
  S=$(aws codebuild batch-get-builds --region "$REGION" --ids "$BID" --query 'builds[0].buildStatus' --output text)
  [ "$S" != IN_PROGRESS ] && break
  printf '.'; sleep 15
done
echo " $S"
[ "$S" = SUCCEEDED ] || { echo "build failed: see CodeBuild logs for $BID"; exit 1; }
echo "== ready. bucket: s3://$BUCKET   next: ./run.sh $REGION s3://<your-bucket>/<video>.mp4"
