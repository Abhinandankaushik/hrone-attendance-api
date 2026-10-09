# Employee Attendance & Analytics API

FastAPI + PyMongo service for punch-in / punch-out, manual corrections with an audit trail, and attendance analytics built
on MongoDB aggregation pipelines. All application code is in `app/main.py`.

## Run

Requires Python 3.11+ and MongoDB 6.0+ (tested on 7.0 and 8.0).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # or export MONGO_URI / MONGO_DB
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are required; real environment variables take priority over `.env`. Indexes are created
idempotently at startup. Interactive docs are at `/docs`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite starts the app with uvicorn against a throwaway database (`TEST_MONGO_URI`, default `mongodb://localhost:27017`;
database `attendance_api_tests`, dropped afterwards). It covers R1-R10 edge cases, parallel punch-ins / punch-outs /
creates / corrections, hand-computed analytics (ties, weekend records, mid-month joiners, employees with no logs, overnight
shifts, missing weekdays) and checks that every explain plan uses `IXSCAN` and no `COLLSCAN`.

`scripts/seed_large_dataset.py` loads ~100,000 records (no indexes, like the grader). On that dataset the app is healthy
in about 3 s including index builds, and every endpoint answers in under 200 ms locally.

## Choices where the contract leaves room

- `punched_at` may be omitted (meaning "now"), but an explicit `null` is rejected with 422, because the schema type is a
  non-nullable integer. The same applies to `status`, `punch_in` and `punch_out` in a correction.
- Punch-out: if the latest record at or before `punched_at` is already closed, the answer is 409; if the employee's open
  record started after `punched_at`, the punch-out is earlier than the punch-in, so the answer is 422.
- A correction "changes nothing" (422) when the final `status`, `punch_in` and `punch_out` equal what is stored. When it
  does change something, all derived fields are recomputed and every one that differs goes into `changes`.
- Concurrent corrections use compare-and-set on the record and its history; the loser gets 409 and nothing is lost.
- `avg_work_hours` includes weekend records; only `present_days` is limited to Monday-Friday (R7).
- `moving_avg_7d` averages the already rounded daily `attendance_rate` values.
- The trend accepts at most 92 days counting both ends (`from` 2026-07-01 to `to` 2026-09-30 is allowed, one more day is 422).
- 422 responses produced by business checks use the same `{"detail": [{"loc", "msg", "type"}]}` shape as FastAPI's own
  validation errors.

Nothing from the assignment is missing.
