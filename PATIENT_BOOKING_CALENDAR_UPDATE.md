# Patient appointment booking and calendar update

## What changed
- Added a separate authenticated Flask page: `/patient/book-appointment`.
- Patient can open it from an available slot's **Book this appointment** action.
- Booking page shows doctor, specialisation, hospital/city, consultation, date, time, duration and fee.
- Confirm action calls the existing API `POST /appointments` with `slotId` and an `Idempotency-Key`.
- Retry attempts on the same page reuse the same idempotency key.
- Added an availability calendar to the doctor detail section of the patient dashboard.
- Calendar highlights dates with open seats and lets the patient select a day.
- Date filters: all available dates (next 30 days), a particular day, next 3 days, next 7 days, or custom range.
- Existing patient dashboard layout and colour theme are retained; only the doctor availability section was enhanced.
- Doctor dashboard template was not changed.

## Deployment
The new page is a Flask template and the route is in `src/doctor/app.py`. Deploy the updated doctor-dashboard application using the existing CloudFormation/EC2 deployment process. No database migration is needed for this frontend change.

## Booking behavior
The booking page calls the existing API route. It does not claim the seat is held before confirmation. The API remains responsible for validating the slot, enforcing capacity, writing the appointment to RDS, and returning success or conflict. A 409 response is shown to the patient so they can return and choose another slot.

## Important
The slot cards show only open slots returned by `/doctors/{doctorId}/slots`. The calendar's highlighted dates are based on that same response and the current materialized slot horizon (up to 30 days). The booking page's displayed values are for review; the API validates the submitted `slotId` as the source of truth.
