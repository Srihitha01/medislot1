# MediSlot dashboard update

## What changed

- The Flask-served doctor dashboard is `src/doctor/templates/doctor_dashboard.html`.
- The same dashboard template is mirrored at `templates/doctor/index.html` for consistency with the repository's alternate template path.
- Consultation types are now loaded from the doctor's actual database records rather than a hard-coded Quick/Standard/Detailed preset list.
- The consultation table displays the consultation name, duration, fee, capacity and status. It supports editing and activation/deactivation. Internal numeric consultation IDs are not displayed.
- Weekly availability displays the consultation name and supports Edit and Delete actions in the table.
- Editing availability sends `PUT /doctors/me/availability-patterns/{pattern_id}`; creating sends POST; deleting sends DELETE. Existing pattern IDs are retained on update.
- Daily schedule and appointment-slot tables display consultation names rather than consultation type IDs. The slot ID column was removed from the doctor-facing slot table.
- Consultation names load before availability and schedule rows so the ID-to-name lookup is ready before rendering.
- The dashboard continues using the existing API Gateway catch-all route and the existing doctor-dashboard deployment workflow.

## Backend safety changes

- Availability updates first verify that the pattern belongs to the authenticated doctor. This avoids treating MySQL `rowcount == 0` as a missing record when the doctor saves unchanged values.
- An existing availability pattern can still be edited while its consultation type is inactive, provided the edit retains that same inactive consultation type. Inactive types cannot be assigned to new patterns or substituted into another pattern.
- Consultation fee updates now enforce the same DECIMAL(10,2) upper bound as consultation creation.
- Successful availability create/update/delete and consultation updates continue through the existing materialiser trigger. The materialiser reconciles future unbooked materialized slots and preserves booked slots.

## Database and deployment

- No database migration or schema change is required.
- Existing sequential numeric IDs and existing records are preserved.
- No CloudFormation changes were necessary: the existing API catch-all accepts PUT/PATCH/DELETE, and the existing GitHub Actions dashboard deployment packages `src/doctor/` including the active template.
- Deploy through the repository's existing GitHub Actions / CloudFormation workflow. No SAM CLI commands are required.

## Validation performed

- Jinja template render: passed.
- Inline JavaScript syntax check (`node --check`): passed.
- Python compile check for API, Flask dashboard and slot materializer modules: passed.
- Materializer and API tests: 14 passed; one unrelated existing patient-authorization test was excluded after it returned 404 where its assertion expects 403.
- Live AWS, API Gateway, RDS and browser end-to-end testing was not performed in this environment.


## Follow-up fixes for the November 10 dashboard deployment

- The latest deployed screenshot showed the previous dashboard even though the ZIP contained the updated template. Root cause: the dashboard job exited successfully when its CloudFormation change set contained no infrastructure changes. That early `exit 0` skipped the later step that publishes `latest-dev.txt` in S3. The EC2 release-pull timer therefore never saw the new application ZIP.
- The workflow now treats "no CloudFormation changes" as "skip only the stack update", then continues to publish the new application release manifest. EC2 can pull and restart the Flask dashboard without replacing the instance.
- The availability form now performs a client-side overlap check against the doctor's loaded weekly patterns before sending POST/PUT. It ignores the pattern currently being edited and uses half-open intervals, so adjacent windows are allowed while genuine overlaps are explained with the existing consultation name, weekday and time.
- The conflicting existing table row is highlighted briefly, directing the doctor to use its Edit action instead of repeatedly submitting an overlapping new window.
- Both `src/doctor/templates/doctor_dashboard.html` (the Flask-served template) and `templates/doctor/index.html` (the mirrored template) are kept in sync.
