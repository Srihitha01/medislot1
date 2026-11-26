# One-time (or whenever iam-stack.yaml changes) IAM stack deploy.
# Needs administrator credentials: the GitHub Actions role is created by this
# stack, so the pipeline cannot deploy it for itself.
# The stack takes no bucket parameter, so this never changes when the network
# stack (and its artifact bucket) is recreated.
param(
    [string]$Environment = "dev",
    [string]$Region = "ap-south-1"
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

aws cloudformation deploy `
  --stack-name "medislot-iam-$Environment" `
  --template-file cloudformation/iam-stack.yaml `
  --capabilities CAPABILITY_NAMED_IAM `
  --parameter-overrides "Environment=$Environment" `
  --region $Region
