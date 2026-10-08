#!/usr/bin/env bash
# Upload the dashboard page and its map config. Safe to re-run; deploy.sh calls it after every deploy,
# and it's all you need after editing site/index.html.
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE=clearhour
REGION=ap-south-1
STACK=clearhour

output() {
  aws cloudformation describe-stacks --stack-name "$STACK" --profile "$PROFILE" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
BUCKET=$(output DataBucketName)
SITE_URL=$(output SiteUrl)
SITE_HOST=${SITE_URL#https://}
SITE_HOST=${SITE_HOST%/}

aws s3 cp site/index.html "s3://$BUCKET/site/index.html" --profile "$PROFILE" --only-show-errors \
  --content-type "text/html; charset=utf-8" --cache-control "max-age=300"

# Amazon Location map key: map tiles only, and only for this page's address. The key ships inside the page,
# so those restrictions are what protect it. It is never printed.
restrictions=$(printf '{"AllowActions":["geo-maps:*"],"AllowResources":["arn:aws:geo-maps:%s::provider/default"],"AllowReferers":["https://%s/*"]}' "$REGION" "$SITE_HOST")
config='{}'
if aws location describe-key --key-name clearhour-map --profile "$PROFILE" >/dev/null 2>&1 \
  || aws location create-key --key-name clearhour-map --no-expiry --restrictions "$restrictions" \
       --profile "$PROFILE" >/dev/null; then
  key=$(aws location describe-key --key-name clearhour-map --profile "$PROFILE" --query Key --output text)
  config=$(printf '{"mapStyle":"https://maps.geo.%s.amazonaws.com/v2/styles/Monochrome/descriptor?key=%s&color-scheme=Light"}' "$REGION" "$key")
else
  echo "Map key not created, so the page falls back to OpenFreeMap tiles." >&2
fi
printf '%s' "$config" | aws s3 cp - "s3://$BUCKET/site/data/config.json" --profile "$PROFILE" --only-show-errors \
  --content-type "application/json" --cache-control "max-age=300"

echo "Dashboard: $SITE_URL"
