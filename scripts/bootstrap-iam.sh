#!/usr/bin/env bash
# One-time (or whenever iam-stack.yaml changes) IAM stack deploy.
# Needs administrator credentials: the GitHub Actions role is created by this
# stack, so the pipeline cannot deploy it for itself.
# The stack takes no bucket parameter, so this never changes when the network
# stack (and its artifact bucket) is recreated.
set -euo pipefail
ENVIRONMENT="${1:-dev}"
REGION="${AWS_REGION:-ap-south-1}"
cd "$(dirname "$0")/.."

aws cloudformation deploy \
  --stack-name "medislot-iam-${ENVIRONMENT}" \
  --template-file cloudformation/iam-stack.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "Environment=${ENVIRONMENT}" \
  --region "${REGION}"
