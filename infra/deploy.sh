#!/usr/bin/env bash
# Model Drift Radar - idempotent deploy with nothing but the AWS CLI.
# No SAM, no CDK, no bootstrap stack. Re-run it as often as you like.
#
#   bash infra/deploy.sh
#
# Two portability notes, learned the hard way on Windows:
#   * Parameter files are written under ./build and passed as RELATIVE
#     file:// paths. A Git Bash /tmp path is not resolvable by aws.exe.
#   * Every structured parameter uses JSON, never CLI shorthand. Shorthand
#     splits on commas, and target lists and JSON payloads both contain them.
set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
TABLE="${RADAR_TABLE:-driftradar}"
AGENT_FN="${RADAR_AGENT_FN:-radar-agent}"
API_FN="${RADAR_API_FN:-radar-api}"
TOPIC_NAME="${RADAR_TOPIC_NAME:-radar-alerts}"
RULE="${RADAR_RULE:-radar-daily-scout}"
RUNTIME="python3.13"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${RADAR_BUCKET:-driftradar-${ACCOUNT}}"

# Regions whose Bedrock catalogs are diffed on every scan.
REGIONS="${RADAR_REGIONS:-us-east-1,us-west-2,eu-west-1,ap-south-1}"

# The dependencies this deployment claims to have. Each is probed on every
# scan, so keep the list to what the application genuinely calls.
WATCH="${RADAR_WATCH:-bedrock:us.amazon.nova-micro-v1:0,bedrock:us.amazon.nova-lite-v1:0,bedrock:us.amazon.nova-pro-v1:0,comprehend:pii@us-east-1,comprehend:sentiment@us-east-1,comprehend:sentiment@ap-south-1,translate:es@us-east-1}"

# Contracts the daily sweep replays. A contract whose targets are unreachable
# would just burn a scheduled run producing a table of ERROR rows.
LIVE_CONTRACTS="${RADAR_LIVE_CONTRACTS:-pii-redaction,pii-detection,sentiment-stability,translate-fidelity}"

# Optional: a generative target that rewrites the migration notes. Empty means
# the deterministic sentences stand, which is the correct default - the tool
# must never depend on the thing it exists to test.
WRITER="${RADAR_WRITER:-}"

FAILED=0
say(){ printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok(){  printf '   \033[32mok\033[0m %s\n' "$1"; }
bad(){ printf '   \033[31mFAILED\033[0m %s\n' "$1"; FAILED=$((FAILED+1)); }
info(){ printf '   \033[2m%s\033[0m\n' "$1"; }
# Report what actually happened rather than assuming success.
try(){ local what="$1"; shift; if out="$("$@" 2>&1)"; then ok "$what";
       else bad "$what"; printf '      %s\n' "$(echo "$out" | head -3)"; fi; }

mkdir -p build
say "Account ${ACCOUNT} / ${REGION}"

# The run key must survive a redeploy, or every previously issued curl command
# silently starts returning 401.
RUN_KEY="${RADAR_RUN_KEY:-}"
if [ -z "$RUN_KEY" ]; then
  RUN_KEY="$(aws lambda get-function-configuration --function-name "$API_FN" \
    --region "$REGION" --query 'Environment.Variables.RADAR_RUN_KEY' \
    --output text 2>/dev/null)"
  [ "$RUN_KEY" = "None" ] && RUN_KEY=""
fi
if [ -z "$RUN_KEY" ]; then
  RUN_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
  info "generated a new run key"
else
  info "reusing the existing run key"
fi

# ---------------------------------------------------------------- storage --
say "DynamoDB table ${TABLE}"
if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
  ok "exists"
else
  try "created table" aws dynamodb create-table --table-name "$TABLE" --region "$REGION" \
    --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
    --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST
  aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
  aws dynamodb update-time-to-live --table-name "$TABLE" --region "$REGION" \
    --time-to-live-specification "Enabled=true,AttributeName=expires_at" >/dev/null 2>&1
  info "on-demand billing, TTL on expires_at"
fi

say "S3 bucket ${BUCKET}"
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  ok "exists"
else
  try "created bucket" aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null
  info "private - raw output is served through the API Lambda, never directly"
fi

say "SNS topic ${TOPIC_NAME}"
TOPIC_ARN="$(aws sns create-topic --name "$TOPIC_NAME" --region "$REGION" \
  --query TopicArn --output text)"
[ -n "$TOPIC_ARN" ] && ok "$TOPIC_ARN" || bad "sns topic"
if [ -n "${RADAR_ALERT_EMAIL:-}" ]; then
  aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol email \
    --notification-endpoint "$RADAR_ALERT_EMAIL" --region "$REGION" >/dev/null
  info "confirmation email sent to ${RADAR_ALERT_EMAIL}"
fi

# -------------------------------------------------------------------- IAM --
# Every AI action here is read-only inference on someone else's model, so the
# blast radius is spend, not data. Scoped to the exact calls the providers make.
cat > build/agent-policy.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:Converse",
   "bedrock:ListFoundationModels","bedrock:ListInferenceProfiles",
   "bedrock:GetFoundationModelAvailability","bedrock:GetFoundationModel"],
  "Resource":"*"},
 {"Effect":"Allow","Action":["comprehend:DetectPiiEntities",
   "comprehend:DetectSentiment","comprehend:DetectDominantLanguage",
   "comprehend:DetectEntities","comprehend:DetectKeyPhrases"],
  "Resource":"*"},
 {"Effect":"Allow","Action":"translate:TranslateText","Resource":"*"},
 {"Effect":"Allow","Action":["servicequotas:ListServiceQuotas",
   "servicequotas:GetServiceQuota"],"Resource":"*"},
 {"Effect":"Allow","Action":["dynamodb:PutItem","dynamodb:GetItem",
   "dynamodb:Query","dynamodb:DeleteItem"],
  "Resource":"arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${TABLE}"},
 {"Effect":"Allow","Action":["s3:PutObject","s3:GetObject"],
  "Resource":"arn:aws:s3:::${BUCKET}/*"},
 {"Effect":"Allow","Action":"sns:Publish","Resource":"${TOPIC_ARN}"}]}
JSON
cat > build/api-policy.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["dynamodb:GetItem","dynamodb:Query"],
  "Resource":"arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${TABLE}"},
 {"Effect":"Allow","Action":"s3:GetObject",
  "Resource":"arn:aws:s3:::${BUCKET}/*"},
 {"Effect":"Allow","Action":"lambda:InvokeFunction",
  "Resource":"arn:aws:lambda:${REGION}:${ACCOUNT}:function:${AGENT_FN}"}]}
JSON

make_role(){ # name, relative policy file
  local name="$1" policy="$2"
  if ! aws iam get-role --role-name "$name" >/dev/null 2>&1; then
    aws iam create-role --role-name "$name" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null \
      && ok "created role $name" || bad "create role $name"
    aws iam attach-role-policy --role-name "$name" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole >/dev/null
  else
    ok "role $name exists"
  fi
  try "inline policy on $name" aws iam put-role-policy --role-name "$name" \
    --policy-name "${name}-inline" --policy-document "file://${policy}"
}

say "IAM roles"
make_role "${AGENT_FN}-role" "build/agent-policy.json"
make_role "${API_FN}-role"   "build/api-policy.json"
AGENT_ROLE="arn:aws:iam::${ACCOUNT}:role/${AGENT_FN}-role"
API_ROLE="arn:aws:iam::${ACCOUNT}:role/${API_FN}-role"

# ---------------------------------------------------------------- package --
say "Package"
python scripts/package.py | tail -2

python - "$TABLE" "$BUCKET" "$TOPIC_ARN" "$REGIONS" "$WATCH" "$LIVE_CONTRACTS" \
         "$WRITER" "$AGENT_FN" "$RUN_KEY" "$REGION" <<'PY'
import json, sys
t, b, topic, regions, watch, live, writer, agent_fn, key, region = sys.argv[1:11]
json.dump({"Variables": {
    "RADAR_TABLE": t, "RADAR_BUCKET": b, "RADAR_TOPIC": topic,
    "RADAR_REGIONS": regions, "RADAR_WATCH": watch,
    "RADAR_LIVE_CONTRACTS": live, "RADAR_WRITER": writer,
    "RADAR_REGION": region}}, open("build/agent-env.json", "w"))
json.dump({"Variables": {
    "RADAR_TABLE": t, "RADAR_BUCKET": b, "RADAR_AGENT_FN": agent_fn,
    "RADAR_RUN_KEY": key, "RADAR_REGION": region}},
    open("build/api-env.json", "w"))
PY

deploy_fn(){ # name zip handler role timeout memory envfile [concurrency]
  local name="$1" zip="$2" hnd="$3" role="$4" to="$5" mem="$6" envf="$7" conc="${8:-}"
  if aws lambda get-function --function-name "$name" --region "$REGION" >/dev/null 2>&1; then
    try "code $name" aws lambda update-function-code --function-name "$name" \
      --region "$REGION" --zip-file "fileb://${zip}"
    aws lambda wait function-updated --function-name "$name" --region "$REGION"
    try "config $name" aws lambda update-function-configuration --function-name "$name" \
      --region "$REGION" --timeout "$to" --memory-size "$mem" \
      --environment "file://${envf}"
    aws lambda wait function-updated --function-name "$name" --region "$REGION"
  else
    # A freshly created role is not always usable yet; retry through propagation.
    local n=0
    until aws lambda create-function --function-name "$name" --region "$REGION" \
        --runtime "$RUNTIME" --role "$role" --handler "$hnd" \
        --zip-file "fileb://${zip}" --timeout "$to" --memory-size "$mem" \
        --environment "file://${envf}" >/dev/null 2>&1; do
      n=$((n+1))
      [ "$n" -ge 6 ] && { bad "create $name (role propagation timed out)"; return 1; }
      info "waiting for IAM propagation ($n)"; sleep 10
    done
    aws lambda wait function-active --function-name "$name" --region "$REGION"
    ok "created $name"
  fi
  # A concurrency cap is a spend guard rail, not a requirement. Accounts with a
  # small total concurrency limit cannot reserve any at all, and refusing to
  # deploy over that would be the tail wagging the dog.
  if [ -n "$conc" ]; then
    if aws lambda put-function-concurrency --function-name "$name" \
       --region "$REGION" --reserved-concurrent-executions "$conc" >/dev/null 2>&1; then
      ok "reserved concurrency ${conc} on $name"
    else
      info "could not reserve concurrency on $name (account limit too low) - skipping"
    fi
  fi
}

say "Lambda functions"
deploy_fn "$AGENT_FN" build/agent.zip handler.lambda_handler "$AGENT_ROLE" 600 512 build/agent-env.json 2
deploy_fn "$API_FN"   build/api.zip   handler.lambda_handler "$API_ROLE"    30 256 build/api-env.json

# ------------------------------------------------------------ function URL --
say "Function URL"
if ! aws lambda get-function-url-config --function-name "$API_FN" --region "$REGION" >/dev/null 2>&1; then
  try "created url" aws lambda create-function-url-config --function-name "$API_FN" \
    --region "$REGION" --auth-type NONE
else
  ok "url config exists"
fi
# Asserted on every run, not only on creation. A URL created by an earlier,
# half-finished deploy has no resource policy, and the only symptom is a
# blanket 403 that looks nothing like a missing permission.
#
# Both statements are required. InvokeFunctionUrl alone documents as
# sufficient and is not: without InvokeFunction the URL answers 403 to
# everyone, with no hint that a permission is what is missing.
grant(){ # statement-id, action, extra args...
  local sid="$1" action="$2"; shift 2
  if aws lambda add-permission --function-name "$API_FN" --region "$REGION" \
     --statement-id "$sid" --action "$action" --principal "*" "$@" \
     >/dev/null 2>&1; then
    ok "granted $action"
  else
    ok "$action already granted"
  fi
}
grant FunctionURLAllowPublicAccess lambda:InvokeFunctionUrl \
  --function-url-auth-type NONE
grant PublicInvokeFunction lambda:InvokeFunction
URL="$(aws lambda get-function-url-config --function-name "$API_FN" --region "$REGION" \
  --query FunctionUrl --output text 2>/dev/null)"
[ -n "$URL" ] && ok "$URL" || bad "function url"

# ------------------------------------------------------------- scheduling --
say "Daily sweep schedule"
RULE_ARN="$(aws events put-rule --name "$RULE" --region "$REGION" \
  --schedule-expression 'cron(0 9 * * ? *)' \
  --description 'Model Drift Radar: daily reachability scan + contract replay' \
  --query RuleArn --output text 2>/dev/null)"
aws lambda add-permission --function-name "$AGENT_FN" --region "$REGION" \
  --statement-id "${RULE}-invoke" --action lambda:InvokeFunction \
  --principal events.amazonaws.com --source-arn "$RULE_ARN" >/dev/null 2>&1

python - "$REGION" "$ACCOUNT" "$AGENT_FN" <<'PY'
import json, sys
region, account, fn = sys.argv[1:4]
json.dump([{"Id": "1",
            "Arn": f"arn:aws:lambda:{region}:{account}:function:{fn}",
            "Input": json.dumps({"mode": "sweep", "trigger": "schedule"})}],
          open("build/targets.json", "w"))
PY
try "daily 09:00 UTC sweep target" aws events put-targets --rule "$RULE" \
  --region "$REGION" --targets file://build/targets.json

# ------------------------------------------------------------------ done --
if [ "$FAILED" -gt 0 ]; then
  printf '\n\033[31m%s step(s) failed.\033[0m\n\n' "$FAILED"
else
  printf '\n\033[1mDeployed cleanly\033[0m\n'
fi
cat <<SUMMARY

  Dashboard   ${URL}
  Run key     ${RUN_KEY}

  Scan reachability now:
    aws lambda invoke --function-name ${AGENT_FN} --region ${REGION} \\
      --cli-binary-format raw-in-base64-out --payload '{"mode":"scan"}' out.json

  Full sweep (scan + every live contract):
    aws lambda invoke --function-name ${AGENT_FN} --region ${REGION} \\
      --cli-binary-format raw-in-base64-out --payload '{"mode":"sweep"}' out.json

  Trigger one contract over HTTP:
    curl -X POST "${URL}api/run" -H "x-radar-key: ${RUN_KEY}" \\
      -d '{"contract":"pii-detection"}'

SUMMARY
exit $([ "$FAILED" -gt 0 ] && echo 1 || echo 0)
