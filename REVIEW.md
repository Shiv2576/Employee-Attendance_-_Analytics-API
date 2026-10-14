# REVIEW.md

Defects found in the starter `app/main.py`. Line numbers refer to the **original** starter. Every "how you'd notice it"
was reproduced against the running starter or checked with an assert against the fixed helpers.

## Configuration and startup

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | module, L22-23 | `MONGO_URI` / `MONGO_DB` fall back to hard-coded `localhost` / `attendance_db`. If the grader's variable is missing, the app silently uses the wrong database instead of failing. | Unset `MONGO_DB`: the app starts and serves from `attendance_db`. | `os.environ[...]`, so a missing variable fails fast. `load_dotenv()` keeps `override=False`, so real env vars still win over `.env`. |
| 2 | module, L22 | `MongoClient` is not `tz_aware`, so PyMongo returns **naive** datetimes. Any arithmetic against an aware IST datetime raises `TypeError` (500), and naive values are easy to misread as local time. | `compute_overtime(record["punch_out"], ...)` with a stored record raises `can't subtract offset-naive and offset-aware datetimes`. | `MongoClient(..., tz_aware=True, tzinfo=timezone.utc)`. `dt_to_ms` also treats any naive value as UTC. |
| 3 | module | No indexes are created. There is no unique key on `employees.emp_code` or on `(emp_code, date)`, and nothing backs the list sort. The grader's DB starts with only `_id`. | `explain()` on `/attendance`'s query shows `COLLSCAN`. Duplicates are possible (see #13, #18). | `create_indexes()` in the FastAPI lifespan (idempotent): `emp_code` unique, `(department, emp_code)`, `(emp_code, date)` unique, `(date -1, emp_code 1)`. All four list-query shapes now plan as `IXSCAN`. |

## Helpers

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 4 | `compute_late_minutes` L35 | `punch_in.replace(hour=h, minute=m)` builds the shift start in **the punch's own time zone**, not IST. With UTC (or server-local) datetimes, the shift start lands 5h30 off. | Punch-in 10:05 IST = 04:35 UTC. Shift start becomes 09:30 **UTC**, so the result is 0 instead of 35. | The shift start is built in IST from the attendance date: `shift_bound(date_str, shift_start)`. |
| 5 | `compute_late_minutes` L36-37 | The grace check is done on minutes truncated by `int()`: `minutes > 10`. R2 says late when **strictly more than 10:00**, so the check must use seconds. | Shift 09:30, punch-in 09:40:01 gives `int(10.016)=10`, not `> 10`, so 0 (expected 10). The same happens for anything up to 09:40:59. | `seconds > 600` decides lateness, then `seconds // 60` (floor). |
| 6 | `compute_late_minutes` | Overnight shifts are ignored. A punch-in after midnight for a 22:00 shift is compared with 22:00 **of the same calendar day**. | Shift 22:00-06:00, punch-in 00:30 IST. Starter gives 0; expected 150. | Takes the R1 attendance date. The shift start is that date at `shift_start`. |
| 7 | `compute_work_hours` L41 | `round(x, 2)` is float/banker's rounding. R4 requires half-up. | 4050 s = 1.125 h: `round(1.125, 2)` gives **1.12**, expected 1.13. | `Decimal(seconds) / 3600`, quantized with `ROUND_HALF_UP`. |
| 8 | `compute_overtime` L47 | No 30-minute minimum (R3). Any positive overtime is returned. | Sample record 1: punch-out 18:35 for an 18:30 end gives 5, expected 0. | Returns the minutes only if `>= 30`. |
| 9 | `compute_overtime` L46 | Overnight shifts: `shift_end` is placed on the attendance date, but an overnight shift ends on the **next** day. | Sample record 7 (22:00-06:00, out 06:40 next day) gives 1480 minutes, expected 40. | `next_day=is_overnight(start, end)`. The signature now takes `shift_start` to detect this. |
| 10 | all helpers / punch-in | Instants are never truncated to whole seconds (R1). Milliseconds leak into the stored and returned value and into the arithmetic. | `punched_at=...900` is stored and returned with `.900`. | `ms_to_dt()` floors to whole seconds before anything is computed or stored. |

## `GET /health`

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 11 | `health` L72-74 | Always returns 200, even when MongoDB is down. The contract requires 503. | `docker stop mongo`, then `/health` still answers 200. | `client.admin.command("ping")`, with a 3 s server-selection timeout. Any `PyMongoError` returns 503. |

## `POST /employees`

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 12 | `EmployeeIn` L53-60 | No validation: `emp_code` pattern, email, name and department lengths, `HH:MM` shifts, a real `joined_on` date, and `shift_start != shift_end` are all missing. | `{"emp_code":"x","name":"","email":"no","joined_on":"nope","shift_start":"25:99"}` returned **201** and was stored. | `Field` patterns and lengths from the contract, a `joined_on` validator (rejects `2026-02-30`), and a model validator for equal shifts. All of these return 422. |
| 13 | `create_employee` L79-83 | Check-then-insert race. Two concurrent creates both pass `find_one` and both insert. | 20 parallel identical creates: more than one 201 and duplicate rows. | Unique index on `emp_code`. `insert_one` is wrapped, and `DuplicateKeyError` returns 409. Re-run: 1×201, 19×409. |
| 14 | `create_employee` L82 | `created_at = datetime.now()` is **naive local time**. PyMongo stores it as if it were UTC, so it is off by the server's UTC offset. The response returns an ISO string, but the contract says epoch ms. | On an IST machine, the stored `created_at` is 5h30 in the future. The response shows `"created_at":"2026-10-14T01:15:29.605422"`. | `now_utc()` (aware, whole seconds). `out_employee` converts it to epoch ms. |

## `GET /employees`

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 15 | `list_employees` L93 | `skip = page * page_size` skips a whole page. Page 1 returns the second page. | `?department=Engineering` returned `items: []` (2 Engineering employees exist). | `skip = (page - 1) * page_size`. |
| 16 | `list_employees` L94 | `total` counts the whole collection, ignoring the filter (R10). | `?department=nope` gives `total: 7` with no items. | `count_documents(q)`. |
| 17 | `list_employees` L95 | No sort. The contract says `emp_code` ascending, and paging without a sort is non-deterministic. | `?page_size=2` returned EMP0003 and EMP0004 first. | `.sort("emp_code", 1)`, backed by the unique index. |
| 18 | `list_employees` L89 | `page` and `page_size` are unbounded. `page=0` and `page_size=1000` are accepted, and a negative page gives a negative skip and a 500. | `?page=0&page_size=1000` gives 200 with every row. | `Query(1, ge=1)` and `Query(20, ge=1, le=100)` return 422. |
| 19 | `list_employees` L91 | `if department:` treats `department=` (empty string) as "no filter". | `?department=` returns everyone. | `if department is not None`. |

## `POST /attendance/punch-in`

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 20 | `punch_in` L101, L113 | An unknown employee is not checked. `emp` is `None`, so `emp["shift_start"]` raises `TypeError`, giving a 500. | `{"emp_code":"EMP9999",...}` gives **500 Internal Server Error**. | Returns 404 when the employee is missing. |
| 21 | `PunchInIn.status` L66 | `status` is a free string. `ABSENT`, `LEAVE` or `"foo"` would be stored, but only `PRESENT`, `WFH` and `ON_DUTY` are allowed. | `status:"ABSENT"` passes validation. | `Literal["PRESENT","WFH","ON_DUTY"]` returns 422. |
| 22 | `PunchInIn.punched_at` L65 | `Optional[int]` in pydantic's lax mode accepts `"1783312500000"` (a string), `1783312500000.0` (a float) and `true`, and does no range check, so seconds-like values pass. | `punched_at: 1783312500` (seconds) is accepted and dated 1970. | `StrictInt` with `ge=100000000000`, `le=4102444800000` returns 422 for seconds, floats, strings and booleans. |
| 23 | `punch_in` L102 | `if body.punched_at` is a truthiness test, so `0` would mean "now". (This is now unreachable because of #22, but it is still wrong.) | — | `is not None`. |
| 24 | `punch_in` L102-103 | `datetime.fromtimestamp(ms / 1000)` gives a **naive server-local** time. The attendance `date` is the server's local date, not the IST date (R1). PyMongo then stores the naive local time as UTC, which shifts the instant. Float division also keeps the milliseconds. | On a UTC server, a punch at 02:00 IST is dated the previous day. On an IST laptop, the stored `punch_in` is 5h30 off. | `ms_to_dt()` (aware UTC, whole seconds). The date comes from `attendance_date()` in IST. |
| 25 | `punch_in` L103 | The overnight-shift date rule (R1) is not implemented. | Shift 22:00-06:00, punch at 00:30 IST on the 10th: expected date `…-09`, starter gives `…-10`. | `attendance_date()` moves the punch back one day when the IST time is before `shift_end` on an overnight shift. Verified: date `2026-07-09`, late 150. |
| 26 | `punch_in` L104-118 | Check-then-insert race. Two identical concurrent punch-ins both see no record and both insert. | 20 parallel identical punch-ins: several 201s and duplicate `(emp_code, date)` rows. | Unique `(emp_code, date)` index. The insert catches `DuplicateKeyError` and returns 409. Re-run: exactly 1×201, 19×409. |
| 27 | `punch_in` L119-121 | The response has an `id` (the ObjectId) the contract forbids, and `punch_in` is an ISO string instead of epoch ms. | The response contains `"id": "..."` and `"punch_in": "2026-..."`. | `out_record()` builds the exact contract shape. |
| 28 | `punch_in` L102 | `datetime.now()` (naive local) is used when `punched_at` is omitted. It has the same zone problem as #24. | — | `now_utc()`. |

## `GET /attendance`

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 29 | `list_attendance` L144-147 | It loads **every matching document into memory**, sorts in Python, then slices. That is O(n) memory and time on 100k records, with no index use. | With no filters, latency and memory grow with the collection. `explain` shows `COLLSCAN`. | Server-side `.sort().skip().limit()`, `count_documents(q)` for `total`, and the `(date -1, emp_code 1)` and `(emp_code, date)` indexes (all `IXSCAN`). |
| 30 | `list_attendance` L145 | The sort is by `date` only. The contract says `date` desc, **then `emp_code` asc**. Ties come back in arbitrary order, and pages can overlap or skip rows. | Several employees on one date come back in insertion order. | `sort([("date", -1), ("emp_code", 1)])`. |
| 31 | `list_attendance` L149 | It leaks `id` (the ObjectId). `punch_in`, `punch_out` and `history[].at` / `changes.punch_*` are ISO strings instead of epoch ms. | `"punch_in":"2026-07-14T04:02:00", "id":"6ace…"`. | `out_record()` converts every instant, including the nested history ones. |
| 32 | `list_attendance` L149 | Legacy records without `history` / `half_day` (and possibly `late_minutes` / `overtime_minutes`) are returned with those **fields missing**. The contract requires them. | Sample record 9 (EMP0002, 2026-07-14) came back without `half_day` and `history`. | Defaults: `[]`, `false`, `0`. |
| 33 | `list_attendance` L126-131 | No validation: malformed dates, `date_from > date_to`, a `status` outside the enum, and `page` / `page_size` bounds. `page=0` silently returned an empty page. | `?date_from=2026-07-09&date_to=2026-07-01` gives 200 with empty items. Expected 422. | `check_date()` (a real `YYYY-MM-DD`), a `date_from <= date_to` check, `status: Literal[...]` and page bounds, all returning FastAPI-shaped 422s. |
| 34 | `check_date` (added in fix #33) | Found during Part B. The first fix validated dates with `len(v) == 10` plus `date.fromisoformat`. On Python 3.11+ `fromisoformat` also accepts ISO week dates, and `"2026-W01-1"` is exactly 10 characters, so it passed. It was then compared as a string against `YYYY-MM-DD` values, which silently gave wrong ranges. | `/attendance?date_from=2026-W01-1` gave **200**; `date.fromisoformat("2026-W01-1")` returns `2025-12-29`. | Also checks `DATE_RE` (`^\d{4}-\d{2}-\d{2}$`) before `fromisoformat`. The same helper now validates the PATCH path date and the trend `from` / `to`. |

## Looked at and decided **not** a defect

- **`load_dotenv()` without `override`.** The default `override=False` already lets real environment variables win, which is what the contract asks for.
- **Sync `def` endpoints with PyMongo.** FastAPI runs them in its threadpool, and `MongoClient` is thread-safe with its own connection pool. The concurrency tests above (20 parallel requests) behave correctly.
- **`doc.pop("_id")` after `insert_one` in `create_employee`.** `insert_one` mutates the dict, and popping `_id` is the right way to keep it out of the response. It is kept inside `out_employee`.
- **Date range comparison on strings (`$gte` / `$lte` on `"YYYY-MM-DD"`).** This is correct, because zero-padded ISO dates sort lexicographically, provided the inputs are validated (#33).
- **`IST = timezone(timedelta(hours=5, minutes=30))`.** This is correct. IST has no DST, so a fixed offset is fine.
- **Comparing `"HH:MM"` strings for `shift_end <= shift_start`.** This is correct, because the format is zero-padded 24-hour and is validated on create.
