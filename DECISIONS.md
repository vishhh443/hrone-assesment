# Decisions

1. **Indexes.** Unique `employees.emp_code` enforces identity. `employees.joined_on` and `department, joined_on` support headcount and department filters. Unique `attendance_logs.emp_code, date` prevents duplicate days and supports employee lookups. `date desc, emp_code asc` serves listings; `date, late_minutes, emp_code` serves late scans. I rejected a `status`-first index because it cannot support date ordering when status is unfiltered.
2. **Punch-in race.** Both requests can pass the employee lookup, but MongoDB's unique compound attendance index accepts only one insert for that employee/date. The winner receives 201; the duplicate-key loser receives 409.
3. **Ties.** The leaderboard uses MongoDB's `$rank` on total late minutes, then filters `rank <= limit`. Every employee tied at the cutoff rank is included; the next rank is skipped. Employee code only makes output order deterministic.
4. **Headcount.** The department summary starts with eligible employee documents and looks up each employee's logs. It counts each employee before aggregating log metrics, so an employee with no matching logs still contributes one to headcount.
5. **100x data.** I would first measure real query plans, then consider pre-aggregated summaries with a controlled refresh. The current design makes no benchmark claims.
