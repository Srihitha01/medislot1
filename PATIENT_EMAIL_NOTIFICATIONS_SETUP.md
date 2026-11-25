# MediSlot patient email notifications (Amazon SES)

This update wires the existing patient booking/cancellation notification flow to Amazon SES.

## What changed

- The sender address is read from the GitHub Actions secret `MEDISLOT_SES_FROM_EMAIL` during the API stack deployment. It is not written into Python, YAML, or this repository.
- The API CloudFormation template declares the sender as a `NoEcho` parameter and injects it into the Lambda environment. The Lambda environment remains encrypted with the function's configured KMS key.
- CloudFormation creates the SES email identity in `ap-south-1`.
- The API stack attaches a narrowly scoped `ses:SendEmail` permission to the existing application-router Lambda role.
- The application uses the injected `SES_FROM_EMAIL` value first and retains the previous SSM lookup as a backwards-compatible fallback.
- The network template already defines the SES API interface endpoint for the private Lambda subnets. No NAT Gateway is added.
- Existing in-app patient notification persistence remains in place. Email failures are logged and do not roll back a committed booking/cancellation.

## 1. GitHub repository secret

In `Srihitha01/medislot1`, open **Settings → Secrets and variables → Actions → New repository secret**.

- Name: `MEDISLOT_SES_FROM_EMAIL`
- Value: the sender mailbox address you control and want SES to send from.

Do not add the email address to source files or commit it.

## 2. Update the existing IAM stack once

The workflow intentionally verifies (rather than creates/updates) the IAM stack because that stack contains the GitHub deployment role used by the workflow itself. Before the first API deployment with this update, update the existing IAM stack so that its GitHub deployment role can manage the SES identity resource.

Run these commands in Windows PowerShell from the repository root, using AWS CLI credentials authorized to update the existing IAM stack:

```powershell
$Region = "ap-south-1"
$NetworkStack = "medislot-network-dev"
$IamStack = "medislot-iam-dev"
$ArtifactBucket = aws cloudformation describe-stacks --stack-name $NetworkStack --region $Region --query "Stacks[0].Outputs[?OutputKey=='DeploymentArtifactsBucketName'].OutputValue | [0]" --output text
if (-not $ArtifactBucket -or $ArtifactBucket -eq "None") { throw "Deployment artifact bucket output was not found." }
aws cloudformation deploy --stack-name $IamStack --template-file cloudformation/iam-stack.yaml --capabilities CAPABILITY_NAMED_IAM --parameter-overrides Environment=dev DeploymentArtifactBucketName=$ArtifactBucket --region $Region
if ($LASTEXITCODE -ne 0) { throw "IAM stack update failed." }
```

This is an update to the existing IAM stack; it does not delete or recreate the database or application data. Review the CloudFormation change set/stack events before proceeding.

## 3. Deploy the application update

Commit and push the updated repository files to a branch matched by the existing workflow (`feature/**` or `main`), or start the workflow with **Actions → MediSlot CI/CD → Run workflow**.

The API deployment job checks that `MEDISLOT_SES_FROM_EMAIL` is present. It passes the value to the `SesFromEmail` CloudFormation parameter; the workflow does not print the value.

## 4. Verify the sender mailbox in SES

After the API stack creates `MediSlotSesSenderIdentity`, Amazon SES sends a verification email to the sender mailbox. Open that mailbox and click the SES verification link. The identity must show as verified in SES in **Asia Pacific (Mumbai), `ap-south-1`**.

Useful read-only check:

```powershell
aws sesv2 get-email-identity --email-identity $env:MEDISLOT_SES_FROM_EMAIL --region ap-south-1
```

Set `$env:MEDISLOT_SES_FROM_EMAIL` locally only if you want to run this check from PowerShell; do not save the address in a source file.

## 5. Request SES production access

The account's last reported SES status was `ProductionAccessEnabled: false` (SES sandbox). While the account remains in the sandbox, SES only sends to verified recipient identities (or the SES mailbox simulator). This means ordinary patients cannot all receive email until AWS approves production access.

In SES for `ap-south-1`, request production access and describe the use case as transactional appointment booking, confirmation, and cancellation notifications sent only to the email address registered by the patient. Include how users opt in, how addresses are collected, and how bounces/complaints are handled. Do not send marketing mail through this flow.

## 6. Expected runtime behavior and logs

- `SES_EMAIL_ACCEPTED`: SES accepted the send request; this does not by itself prove final inbox delivery.
- `SES_EMAIL_FAILED`: the Lambda logs the SES/SSM exception. Check sender verification, sandbox recipient verification, IAM permission, endpoint connectivity, and SES account status.
- `SES_EMAIL_SKIPPED ... registered_email_missing`: the patient record did not provide a registered email address.
- `PATIENT_NOTIFICATION_PERSIST_FAILED`: the in-app notification write failed independently of SES.

The recipient continues to be the patient's registered email address looked up by the existing booking/cancellation code. The sender is the configured MediSlot mailbox.

## Important

The ZIP contains the source and deployment configuration; it cannot verify the mailbox or obtain SES production approval on your behalf. Email delivery to arbitrary patient addresses is ready only after the sender verification and SES production-access prerequisites above are completed.
