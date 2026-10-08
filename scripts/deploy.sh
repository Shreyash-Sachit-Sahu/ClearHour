#!/usr/bin/env bash
# Build and deploy the clearhour stack, then upload the model and the PM2.5 archive to its data bucket.
# Parameter values come from .env as overrides on every run; samconfig.toml never holds them.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; source .env; set +a
mask() { sed -e "s/${OPENAQ_API_KEY:-unset-openaq}/[openaq-key]/g" -e "s/${META_ACCESS_TOKEN:-unset-meta}/[meta-token]/g"; }

overrides=("OpenAqApiKey=${OPENAQ_API_KEY}" "WaMode=${WA_MODE:-dry_run}")
for pair in WaPhoneNumberId=WA_PHONE_NUMBER_ID WhatsAppEventsTopicArn=WA_EVENTS_TOPIC_ARN \
    WaTemplateLangEn=WA_TEMPLATE_LANG_EN WaTemplateLangHi=WA_TEMPLATE_LANG_HI \
    MetaPhoneNumberId=META_PHONE_NUMBER_ID MetaAccessToken=META_ACCESS_TOKEN; do
  var="${pair#*=}"
  if [ -n "${!var:-}" ]; then overrides+=("${pair%%=*}=${!var}"); fi  # only parameters with a value
done

sam build
sam deploy --no-fail-on-empty-changeset --parameter-overrides "${overrides[@]}" 2>&1 | mask

bucket=$(aws cloudformation describe-stacks --stack-name clearhour --profile clearhour \
  --query "Stacks[0].Outputs[?OutputKey=='DataBucketName'].OutputValue" --output text)
aws s3 cp models/clearhour-lgbm.txt "s3://${bucket}/models/clearhour-lgbm.txt" --profile clearhour
aws s3 cp models/features.json "s3://${bucket}/models/features.json" --profile clearhour
aws s3 cp data/processed/pm25_hourly.parquet "s3://${bucket}/archive/pm25_hourly.parquet" --profile clearhour
echo "deployed; model and archive uploaded to s3://${bucket}/"
scripts/publish_site.sh
