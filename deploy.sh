#!/usr/bin/env bash
# Deploy the POC stack and build the images. Runs as-is in AWS CloudShell.
#   ./deploy.sh [region]
# Env (all optional):
#   STACK            stack name (default beat-poc)
#   SEGMENTS         Fargate ffmpeg slices (default 8)
#   LAMBDA_SEGMENTS  Lambda ffmpeg slices (default 16)
#   ACCEL            MediaConvert acceleration: PREFERRED (default) | DISABLED
#   GPUS             GPU instance types to test, comma separated, max 5, each type[:gpus[:streams]]
#                    gpus = GPUs per task (0.125 / 0.25 / 0.5 for fractional g6f), streams = parallel slices (default 4)
#                    e.g. GPUS=g4dn.xlarge,g6.xlarge,g6f.xlarge:0.125:2,g6f.2xlarge:0.25:3
#   GPU_ON           1 = one running instance per GPU type (default when GPUS is set), 0 = scale to zero
#   SKIP_BUILD=1     reuse the images already in ECR
set -euo pipefail
REGION=${1:-${AWS_REGION:-us-west-1}}
STACK=${STACK:-beat-poc}
cd "$(dirname "$0")"

GP=()
IFS=',' read -ra L <<< "${GPUS:-}"
for i in 1 2 3 4 5; do
  IFS=':' read -r t g st <<< "${L[$((i - 1))]:-}"
  GP+=("Gpu${i}Type=${t:-}" "Gpu${i}Gpus=${g:-1}" "Gpu${i}Streams=${st:-4}")
done
GPU_ON=${GPU_ON:-$([ -n "${GPUS:-}" ] && echo 1 || echo 0)}

stackq() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" --query "$1" --output text; }
out() { stackq "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue"; }
deploy() {
  aws cloudformation deploy --region "$REGION" --stack-name "$STACK" --template-file template.yaml \
    --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset --parameter-overrides \
    SegmentCount="${SEGMENTS:-8}" LambdaSegments="${LAMBDA_SEGMENTS:-16}" McAcceleration="${ACCEL:-PREFERRED}" \
    GpuDesired="$GPU_ON" LambdaReady="$1" "${GP[@]}"
}

READY=false
if aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" >/dev/null 2>&1; then
  READY=$(stackq "Stacks[0].Parameters[?ParameterKey=='LambdaReady'].ParameterValue")
  [ "$READY" = true ] || READY=false
fi
echo "== deploying $STACK in $REGION (GPUs: ${GPUS:-none}, running: $GPU_ON)"
deploy "$READY"
BUCKET=$(out BucketName)

if [ -z "${SKIP_BUILD:-}" ]; then
  echo "== building images with CodeBuild (x86: ECS + Lambda, arm64: Lambda)"
  python3 -c "import shutil; shutil.make_archive('/tmp/beat-poc-src', 'zip', '.', 'container')"
  aws s3 cp --region "$REGION" /tmp/beat-poc-src.zip "s3://$BUCKET/build/src.zip" --only-show-errors
  IDS=()
  for p in "$(out BuildX86)" "$(out BuildArm)"; do
    IDS+=("$(aws codebuild start-build --region "$REGION" --project-name "$p" --query build.id --output text)")
  done
  while :; do
    S=$(aws codebuild batch-get-builds --region "$REGION" --ids "${IDS[@]}" --query 'builds[].buildStatus' --output text)
    [[ $S == *IN_PROGRESS* ]] || break
    printf '.'; sleep 20
  done
  echo " $S"
  [[ $S == "SUCCEEDED"*"SUCCEEDED" ]] || { echo "build failed (see CodeBuild logs): ${IDS[*]}"; exit 1; }
fi

if [ "$READY" = true ]; then
  REPO=$(out RepoUri)
  for a in x86 arm64; do
    aws lambda update-function-code --region "$REGION" --function-name "$STACK-$a" --image-uri "$REPO:lambda-$a" >/dev/null
    aws lambda wait function-updated-v2 --region "$REGION" --function-name "$STACK-$a"
  done
else
  echo "== creating Lambda functions"
  deploy true
fi
echo "== ready. bucket: s3://$BUCKET   next: ./run.sh $REGION s3://<bucket>/<video>.mp4"
