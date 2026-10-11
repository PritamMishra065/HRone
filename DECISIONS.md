# DECISIONS.md

1. **Indexes.** `employees.emp_code` is unique for employee identity and lookup. `employees.department,joined_on` and `joined_on` support summary and trend headcount queries. `attendance_logs.emp_code,date` is unique and supports employee-month queries. `date,emp_code` supports ordered attendance lists, while `status,date,emp_code` helps filtered lists. I did not add separate indexes for every status because each additional index slows writes.

2. **Punch-in race.** Both requests build the same natural key, but MongoDB accepts only the first insert because of the unique `(emp_code,date)` index. The winner returns 201. The loser raises `DuplicateKeyError`, which the API turns into 409.

3. **Ties.** The leaderboard uses MongoDB `$rank`, so it uses competition ranking such as 1, 2, 2, 4. It filters by rank after calculating it, therefore every employee tied at the cutoff is returned. Rows are then ordered by late minutes and `emp_code`.

4. **Headcount.** Department summary starts from eligible employees, then looks up their logs. That keeps employees with no matching logs in the result.

5. **At 100x scale**, I would pre-aggregate daily department metrics and use background jobs for historical reports.
