#!/bin/bash
set -e

# Load deploy-time config (GOOGLE_CLIENT_ID, etc.) from .env so a fresh shell
# doesn't synth the Cognito Google IdP with an empty client_id — which Cognito
# rejects with "clientId, clientSecret and authorizeScopes are all required".
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [ -z "$GOOGLE_CLIENT_ID" ]; then
  echo "❌ GOOGLE_CLIENT_ID is not set (check .env). Aborting before it breaks the Google IdP." >&2
  exit 1
fi

echo "�� Building React frontend..."
cd frontend
npm ci
npm run build
cd ..

echo "�� Deploying CDK stack..."
cdk deploy --all --require-approval never

# Belt-and-suspenders: push the built frontend straight to the hosting bucket and
# invalidate CloudFront. CDK's BucketDeployment no-ops when the build hash is
# unchanged, so if the bucket was ever emptied out-of-band it would NOT be restored
# by the deploy above — leaving the site as an S3 AccessDenied page. This guarantees
# the files are present and the cache is fresh on every deploy.
echo "☁️  Syncing frontend to S3 + invalidating CloudFront..."
BUCKET=$(aws cloudformation describe-stack-resources --stack-name CredlyCertTrackerStack \
  --query "StackResources[?ResourceType=='AWS::S3::Bucket' && contains(LogicalResourceId, 'DashboardBucket')].PhysicalResourceId | [0]" --output text)
DIST=$(aws cloudformation describe-stack-resources --stack-name CredlyCertTrackerStack \
  --query "StackResources[?ResourceType=='AWS::CloudFront::Distribution'].PhysicalResourceId | [0]" --output text)
if [ -n "$BUCKET" ] && [ "$BUCKET" != "None" ]; then
  aws s3 sync frontend/dist/ "s3://$BUCKET/" --delete
fi
if [ -n "$DIST" ] && [ "$DIST" != "None" ]; then
  aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/*" >/dev/null \
    && echo "   CloudFront $DIST invalidated"
fi

echo "✅ Done! Dashboard URL:"
aws cloudformation describe-stacks \
  --stack-name CredlyCertTrackerStack \
  --query "Stacks[0].Outputs[?starts_with(OutputKey, 'HostingDashboardUrl')].OutputValue" \
  --output text
