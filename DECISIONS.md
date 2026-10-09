# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (409 on duplicates, every lookup), `(department, emp_code)` for the filtered list, `joined_on` for the headcount match. `attendance_logs`: unique `(emp_code, date)` (one record per day, PATCH, monthly, `$lookup`), `(emp_code, punch_in -1)` for punch-out's "latest punch-in", `(date -1, emp_code)` for the list sort and month scans, `(status, date -1, emp_code)` for status filters. I rejected an index on `late_minutes`: the month's date range already narrows the leaderboard.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets exactly one write succeed; the other gets `DuplicateKeyError` and returns 409. There is no read-before-insert.

3. **Ties.** `$setWindowFields` with `$rank` gives 1, 2, 2, 4. I filter `rank <= limit` after ranking, so both tied employees are returned even if that exceeds `limit`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`), looks up each person's monthly totals and unwinds with `preserveNullAndEmptyArrays`, so people without logs still count once.

5. **100x data.** Keep a monthly per-employee rollup collection updated on every punch or correction, so analytics read thousands of rows, not millions of logs.
