#!/usr/bin/env bash
# Push a sample video through every route side by side, then print the latency / cost report.
#   ./run.sh <region> <s3://src-bucket/video.mp4> [copies]
# Env: ROUTES     comma list from mc,fargate,lambda-x86,lambda-arm64,gpu (default: all)
#      GPU_TYPES  only test these GPU types (default: every GPU slot in the stack)
#      STACK      (default beat-poc)
set -euo pipefail
REGION=$1; SRC=$2; N=${3:-1}
STACK=${STACK:-beat-poc}
ROUTES=${ROUTES:-mc,fargate,lambda-x86,lambda-arm64,gpu}
cd "$(dirname "$0")"
outs=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" --query 'Stacks[0].Outputs' --output json)
o() { python3 -c "import json,sys; print(next((x['OutputValue'] for x in json.loads(sys.argv[1]) if x['OutputKey']==sys.argv[2]), ''))" "$outs" "$1"; }
BUCKET=$(o BucketName); SM=$(o StateMachineArn); CLUSTER=$(o Cluster)

# GPU slots that exist in the stack -> [{"type":..., "taskdef":...}], and wait until each has a registered instance
GPUS_JSON=$(python3 -c "
import json,sys
o=json.loads(sys.argv[1]); g=[dict(zip(('type','taskdef','streams'), x['OutputValue'].split('|'))) for x in o if x['OutputKey'].startswith('GpuTask')]
want=[t for t in sys.argv[3].split(',') if t]
g=[x for x in g if not want or x['type'] in want]
print(json.dumps(g if 'gpu' in sys.argv[2].split(',') else []))" "$outs" "$ROUTES" "${GPU_TYPES:-}")
for t in $(python3 -c "import json,sys; print(' '.join(g['type'] for g in json.loads(sys.argv[1])))" "$GPUS_JSON"); do
  printf "== waiting for a %s container instance" "$t"
  until [ -n "$(aws ecs list-container-instances --region "$REGION" --cluster "$CLUSTER" \
            --filter "attribute:ecs.instance-type == $t" --query 'containerInstanceArns[0]' --output text | grep -v None)" ]; do
    printf '.'; sleep 15
  done
  echo " ok"
done

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
echo "== starting $N execution(s), routes: $ROUTES"
for i in $(seq 1 "$N"); do
  KEY="bench/$RUN-$i-$(basename "$SRC")"
  aws s3 cp --region "$REGION" "$STAGE" "s3://$BUCKET/$KEY" --only-show-errors
  INPUT=$(python3 -c "
import json,sys
r=sys.argv[4].split(',')
print(json.dumps({'bucket':sys.argv[1],'key':sys.argv[2],'id':sys.argv[3],'gpus':json.loads(sys.argv[5]),
  'routes':{'mc':'mc' in r,'fargate':'fargate' in r,'lambdaX86':'lambda-x86' in r,'lambdaArm':'lambda-arm64' in r,'gpu':'gpu' in r}}))
" "$BUCKET" "$KEY" "$RUN-$i" "$ROUTES" "$GPUS_JSON")
  aws stepfunctions start-execution --region "$REGION" --state-machine-arn "$SM" --name "$RUN-$i" --input "$INPUT" >/dev/null
done
python3 report.py --region "$REGION" --stack "$STACK" --since "$SINCE" --expect "$N"
