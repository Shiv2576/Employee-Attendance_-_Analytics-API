# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup (no virtual environment)
```bash
docker run -d --name attendance-mongo -p 27017:27017 mongo:7
pip install -r requirements.txt                      # into the Python that owns your `uvicorn`
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # required: no hard-coded fallback
python sample_seed.py                                # optional: the nine sample documents
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent), and `/health` is ready in about 1 s, even on a ~100k-record database.
MongoDB **6.0 or newer** is required (`$densify`, `$setWindowFields`, `$lookup` with `localField` + `pipeline`).

## Status
Everything in the contract is implemented: Part A (`REVIEW.md`, 34 defects), Part B (punch-out, PATCH, and the four
analytics aggregations), and Part C (`/admin/explain/{endpoint}`, `DECISIONS.md`). Notes:
- Analytics read the stored derived fields. Python only does the final half-up rounding (R8), and the trend's rates
  are rounded half-up inside MongoDB in Decimal128, because `$round` is half-to-even.
- PATCH uses optimistic concurrency: the losing concurrent correction gets 409 and no history entry is lost.
  A PATCH that changes none of `status` / `punch_in` / `punch_out` is 422.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
