#!/bin/bash
set -e

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
  --query "Stacks.Outputs[?OutputKey=='DashboardUrl'].OutputValue" \
  --output text
