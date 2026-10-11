# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always returned 200 without checking MongoDB. | Stop MongoDB, then call `/health`. | Ping MongoDB and return 503 on a driver error. |
| 2 | startup | No required indexes existed. | Duplicate or parallel writes could create duplicates; lists slow down on a large collection. | Create idempotent unique and query indexes at startup. |
| 3 | `EmployeeIn` | It accepted invalid codes, emails, dates, shift times, and equal shifts. | Send `EMP1`, `09:99`, or equal shift times. | Added Pydantic field constraints and a shift validator. |
| 4 | `create_employee` | Check-then-insert was not safe under concurrent requests. | Send two identical creates in parallel. | Enforce a unique index and translate `DuplicateKeyError` to 409. |
| 5 | `create_employee` | Stored a local naive creation time and returned a datetime instead of epoch milliseconds. | Inspect the JSON response. | Store UTC BSON dates and serialize response instants to milliseconds. |
| 6 | `list_employees` | First page skipped records and `total` ignored the department filter. | Request page 1 or filter by a department. | Use `(page - 1) * page_size` and count the same query. |
| 7 | `list_employees` | Results were not sorted by `emp_code`. | Insert codes out of order. | Sort in MongoDB by `emp_code` ascending. |
| 8 | `PunchInIn` | Timestamp and status validation were missing. | Send seconds, a float, `ABSENT`, or `LEAVE`. | Require bounded strict epoch milliseconds and presence-only statuses. |
| 9 | `punch_in` | Unknown employees caused a 500; host-local time was used. | Punch in with an absent code or run on another timezone. | Return 404 and convert timestamps explicitly through UTC. |
| 10 | `punch_in` | Attendance date ignored IST, overnight shifts, second truncation, and concurrent inserts. | Punch in after midnight for a 22:00-06:00 shift or send parallel requests. | Calculate IST attendance day, truncate to seconds, and rely on the unique attendance index. |
| 11 | `compute_late_minutes` | It used the punch date for shift start, so overnight shifts were wrong. | Punch in near midnight on an overnight shift. | Build shift start/end from the attendance day in IST. |
| 12 | `compute_work_hours` | Python float rounding is bankers rounding, not required half-up rounding. | Test an exact half-cent hour boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 13 | `compute_overtime` | It did not handle overnight end dates or the 30-minute threshold. | Punch out 20 minutes late or after an overnight shift. | Build the real shift end and return overtime only at 30 minutes or more. |
| 14 | `punch_in` response | It added an `id`, which is forbidden by the contract. | Inspect a successful punch-in response. | Remove `_id` internally and never expose a replacement id. |
| 15 | `list_attendance` | Loaded every matching document into Python, sorted there, and did not apply secondary sorting. | Query a large dataset or records with the same date. | Use an indexed MongoDB find, sort, skip, limit, and filtered count. |
| 16 | `list_attendance` | Date ranges and paging were not validated. | Send `date_from` later than `date_to`, page 0, or page size 500. | Validate dates and use constrained query parameters. |

I also checked `load_dotenv()`. Its default behavior does not overwrite real environment variables, so it already meets the configuration requirement and was kept.

Also note anything you looked at and decided was **not** a defect, and why.
