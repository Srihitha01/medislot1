# Booking concurrency and refund milestone update

## Included in this update
- Slot reservation uses an optimistic-concurrency `version` attribute and a bounded retry loop.
- Strongly consistent slot reads are used between conflicts. The loop does not serialize Lambda invocations.
- `BookingConflictRetries` CloudWatch metric is emitted from the booking path.
- `AppointmentBooked` is sent to the configured EventBridge bus after the RDS appointment transaction commits.
- Slot materializer initializes `version: 0` when its slot-item shape contains the standard `bookedCount: 0` field.
- Refund policy helper and exact boundary tests use the approved tiers:
  - strictly more than 48 hours: 100%
  - 24 hours through exactly 48 hours: 75%
  - 2 hours through exactly 24 hours: 50%
  - less than 2 hours: 0%
- API router environment includes refund policy parameter name, EventBridge bus name and retry limit.
- Existing dashboard templates are intentionally unchanged.

## Important deployment prerequisites / remaining work
This ZIP is not a claim that every milestone deliverable is complete. The original project does not yet contain a cancellation route, doctor-initiated slot cancellation workflow, or refund-ledger write workflow. The helper and metric are groundwork, not a substitute for those endpoints. `CancellationsByTier` is defined but is only emitted once cancellation processing is wired.
The ApplicationLambdaRole exported by the IAM stack must grant narrowly scoped `events:PutEvents` to the selected event bus and `cloudwatch:PutMetricData`; otherwise those best-effort calls will log failures. Review and update that role policy in CloudFormation before deployment.
Create the SSM String parameter `/medislot/dev/refund/policy` (and corresponding environment parameter) with JSON matching the helper's schema, or the application falls back to the approved defaults. Example:
[
  {"tier":"FULL","minimumHours":48,"refundPercent":100},
  {"tier":"PARTIAL_75","minimumHours":24,"refundPercent":75},
  {"tier":"PARTIAL_50","minimumHours":2,"refundPercent":50},
  {"tier":"NONE","minimumHours":0,"refundPercent":0}
]
The helper's exact boundary behavior is implemented in tests. Run the existing CI pipeline and an AWS-backed parallel booking test before mentor sign-off. A local mocked test cannot prove 20+ live requests contended on one DynamoDB item.
The original `EnableReservedConcurrency` setting must remain false for the contention test; a reserved concurrency of 1 would serialize requests and invalidate the test.
