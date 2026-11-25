# MediSlot patient booking limits

The application API enforces these rules before reserving slot capacity:

1. A patient may have at most **3 active appointments total across all doctors**.
2. A patient may have at most **1 active appointment with a given doctor**, regardless of the appointment date or time.
3. `booked` and `confirmed` appointments count as active.
4. Cancelled and completed appointments do not count.
5. The patient row is locked with `SELECT ... FOR UPDATE` while the checks and booking transaction run. This serializes concurrent booking attempts for the same patient.
6. The checks are backend-enforced. Frontend controls are not the security boundary.

Rejected bookings return HTTP 409 with a rule-specific message. The slot reservation and existing idempotency behavior remain in place.
