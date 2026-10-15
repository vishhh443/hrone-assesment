# Starter-code review

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Module configuration | An absent `MONGO_URI` silently connects to local MongoDB; the client is not TLS-configured, pinged, or closed. | Start without configuration or point at an unreachable server; the old `/health` still claims success and the app may target localhost. | Require `MONGO_URI`, load `.env` without overriding runtime variables, configure an aware/TLS client, initialize indexes and ping on startup/readiness, and close it on shutdown. |
| 2 | `health` | The old endpoint always returned success without checking MongoDB. | Stop MongoDB and request `/health`; it still returned 200. | Ping MongoDB and return 503 when it is unconfigured or unavailable. |
| 3 | `compute_late_minutes`, `punch_in` | Local-naive timestamps and machine-local `fromtimestamp` mixed with IST shift times; late calculation did not anchor overnight shifts to the attendance day. | Run on a non-IST host; an overnight early-morning punch or a timestamp near the grace boundary is classified against the wrong instant. | Convert epoch milliseconds to whole-second UTC instants, derive the IST attendance date (including overnight shifts), and calculate lateness against that shift's start. |
| 4 | `compute_overtime` | It treated the shift end as the attendance date even for overnight shifts and did not apply the 30-minute minimum. | For a 22:00-06:00 shift, calculate overtime at 06:29 and 06:30 the next day. | Build the correct next-day shift end and count only complete overtime once it reaches 30 minutes. |
| 5 | `compute_work_hours` | Python `round()` uses ties-to-even and binary floats can change half-cent cases. | A duration exactly at a two-decimal half boundary can round down unexpectedly. | Use `Decimal` and `ROUND_HALF_UP` on whole-second durations. |
| 6 | `create_employee`, `punch_in` | Existence checks followed by inserts are race-prone, and punch-in dereferenced an unknown employee. | Concurrent duplicate requests can both pass the check; an unknown employee can cause a server error. | Validate the employee, enforce unique indexes, and map duplicate-key errors to 409. |
| 7 | Request models and endpoints | Inputs did not enforce contract formats, timestamp units/ranges, statuses, shift rules, or page limits. | Try a seconds-valued timestamp, unsupported status, invalid date, equal shift times, or page size 101. | Add Pydantic validation for request fields and explicit validation for calendar dates and query ranges. |
| 8 | `list_employees` | It skipped `page * page_size` rows and counted the unfiltered collection. | Page 1 starts at the second page; a department filter returns the wrong `total`. | Use `(page - 1) * page_size` and count the same filtered query used to fetch results. |
| 9 | `list_attendance` | It loaded and sorted the full collection in Python, omitted deterministic tie sorting, and exposed MongoDB `_id` as `id`. | The 100k-record case consumes unnecessary memory; equal dates are unstable and responses contain an internal id. | Filter, sort, count, and paginate in MongoDB; serialize the documented fields only. |
| 10 | Application routes | Punch-out, regularization, monthly/dept analytics, leaderboard, trend, and explain routes were missing. | Requests to any of those contract paths returned 404. | Implement the routes and share pipeline builders between the analytics and explain operations. |
| 11 | Punch-out and regularization | No atomic close/correction operation or audit-trail concurrency protection existed. | Parallel punch-outs can both appear successful; concurrent corrections can overwrite history. | Use a conditional atomic punch-out update and compare-and-append regularization history in one atomic update. |
| 12 | API serialization | Mongo datetimes were not consistently converted to epoch milliseconds, and seeded records may omit optional legacy fields. | Datetimes leak into JSON or `half_day`/`history` is missing from older records. | Convert every API instant, including nested history changes, and supply documented defaults for legacy fields. |

## Reviewed and judged intentional

- Shift times and attendance dates are stored as strings: these are calendar values, not instants, per `DATA_MODEL.md`.
- `shift_end <= shift_start` is a valid overnight shift, not an invalid employee schedule.
- The sample `attendance_logs` intentionally has a legacy record with no `half_day` or `history`; those fields default to `false` and `[]`.
- The sample seeder was destructive because it deleted both collections before loading. It now requires an explicit opt-in and refuses to seed non-empty collections instead of deleting records.

## Verification notes

The attached contract's boundary cases are covered by `tests/test_main.py` where the behavior can be exercised without a server. Aggregation pipelines and index plans must additionally be run against MongoDB 6.0+; no Atlas URI or local MongoDB service was configured during this implementation, so those database-backed checks are not represented as passing.
