# DECISIONS.md

1. **Indexes.** `employees`: `emp_code` unique (duplicate creates become 409; it backs every join), `(department, emp_code)` (the list filter and sort, and the trend's department match), and `(joined_on, department)` (the summary's headcount match). `attendance_logs`: `(emp_code, date)` unique (one record per day, punch-in races, PATCH, punch-out, monthly, and the per-employee `$lookup`s), and `(date -1, emp_code 1)` (the list's sort, and the leaderboard's month range). I rejected an index on `status`: it has five values and is always combined with a date. I also rejected `(emp_code, punch_in)` for punch-out, because date order already equals punch-in order.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets exactly one insert succeed. The other raises `DuplicateKeyError`, which becomes a 409. There is no read-then-write window.

3. **Ties.** `$rank` gives 1, 2, 2, 4. I filter on `rank <= limit`, not on the row count, so everyone tied at the cutoff is returned, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`) and joins the logs to it. `$unwind` with `preserveNullAndEmptyArrays` keeps employees who have no logs.

5. **100x data.** I would maintain per-employee daily or monthly rollups when writing, so analytics stop scanning raw logs. I would also replace skip-based paging with keyset paging.
