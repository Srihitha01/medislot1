# MediSlot patient email notifications (Amazon SNS)

MediSlot uses Amazon SNS standard-topic email subscriptions instead of Amazon SES. No custom sending domain or SES sender identity is configured.

## Architecture

- One topic per environment: `medislot-<environment>-patient-notifications`.
- Each patient subscribes their registered email address to the shared topic.
- Each subscription receives a filter policy on the `patientId` message attribute. The registration and API Lambdas publish the authenticated patient's numeric ID as that attribute.
- The patient must confirm the SNS subscription email before receiving appointment email notifications.
- In-app notifications continue to be saved in the RDS `patient_notifications` table.

## Registration and delivery

1. Patient registration commits the patient and user records to RDS.
2. The registration Lambda requests an SNS email subscription with the patient's numeric `patientId` filter.
3. SNS sends a confirmation email. Registration still succeeds if the SNS request fails; the failure is recorded in CloudWatch Logs.
4. On booking/cancellation, the API saves the in-app notification and ensures the registered email has an SNS subscription.
5. The API publishes the message to the shared topic with the `patientId` message attribute.
6. SNS delivers the message only to the matching patient's confirmed subscription.

The API repeats the idempotent subscription request when sending a notification. This supports patients who registered before SNS was enabled and patients whose registered email address has changed.

## One-time IAM stack update before the next workflow run

The existing GitHub Actions workflow deliberately verifies the IAM stack instead of updating it, because that stack contains the GitHub deployment role used by the workflow itself. Therefore, update the IAM stack once using AWS CLI credentials authorized to update IAM roles and policies. Run from the repository root in Windows PowerShell:

```powershell
$Region = "ap-south-1"
$NetworkStack = "medislot-network-dev"
$IamStack = "medislot-iam-dev"
$ArtifactBucket = aws cloudformation describe-stacks --stack-name $NetworkStack --region $Region --query "Stacks[0].Outputs[?OutputKey=='DeploymentArtifactsBucketName'].OutputValue | [0]" --output text
if (-not $ArtifactBucket -or $ArtifactBucket -eq "None") { throw "Deployment artifact bucket output was not found." }
aws cloudformation deploy --stack-name $IamStack --template-file cloudformation/iam-stack.yaml --capabilities CAPABILITY_NAMED_IAM --parameter-overrides Environment=dev DeploymentArtifactBucketName=$ArtifactBucket --region $Region
if ($LASTEXITCODE -ne 0) { throw "IAM stack update failed." }
```

Review the stack events and confirm `UPDATE_COMPLETE` before pushing the application changes. This updates IAM permissions in place; it does not delete or recreate RDS data.

## CloudFormation and IAM

- `cloudformation/api-stack.yaml` creates the SNS topic and injects `SNS_TOPIC_ARN` into the API Lambda.
- `cloudformation/authentication.yaml` injects the same topic ARN into the registration Lambda.
- `cloudformation/iam-stack.yaml` grants `sns:Subscribe` to the registration role and `sns:Publish` plus `sns:Subscribe` to the API role. The deployment role can manage the topic.
- `cloudformation/network-stack.yaml` creates an SNS interface VPC endpoint. MediSlot Lambda functions run in private subnets without a NAT Gateway.
- The GitHub Actions workflow no longer expects `MEDISLOT_SES_FROM_EMAIL`.

## CloudWatch log events

- `SNS_PATIENT_EMAIL_SUBSCRIPTION_REQUESTED`: registration requested the email subscription.
- `SNS_PATIENT_EMAIL_SUBSCRIPTION_FAILED`: registration could not request the subscription; account creation remains successful.
- `SNS_EMAIL_CONFIRMATION_REQUIRED`: SNS reports that the endpoint is still pending confirmation.
- `SNS_EMAIL_PUBLISH_ACCEPTED`: SNS accepted the notification publish request. It does not prove final inbox delivery.
- `SNS_EMAIL_PUBLISH_FAILED`: SNS publish or subscription setup failed.
- `PATIENT_IN_APP_NOTIFICATION_SAVED`: RDS in-app notification was persisted.
- `PATIENT_NOTIFICATION_PERSIST_FAILED`: the in-app notification write failed independently of email delivery.

## Important SNS behavior

SNS email subscriptions require an explicit confirmation click. Until confirmed, the address will not receive topic notifications. SNS email uses its standard notification format and does not support custom HTML templates or a custom sender domain. SNS email throughput and delivery quotas apply.

Existing patients who registered before this change will be subscribed the next time MediSlot attempts to send them a notification; they must confirm the SNS email before later notifications can be delivered.
