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

echo "✅ Done! Dashboard URL:"
aws cloudformation describe-stacks \
  --stack-name CredlyCertTrackerStack \
  --query "Stacks[0].Outputs[?starts_with(OutputKey, 'HostingDashboardUrl')].OutputValue" \
  --output text
