# Attendance API

FastAPI + MongoDB service. All code lives in `app/main.py`.

## Requirements
- Python 3.10+
- MongoDB 6.0+ (uses `$densify`, `$setWindowFields`, `$lookup` with `localField` + `pipeline`)

## Setup
```bash
# 1. MongoDB (skip if you already have one running)
docker run -d --name attendance-mongo -p 27017:27017 mongo:7

# 2. Dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Configuration (required, no defaults)
export MONGO_URI=mongodb://localhost:27017
export MONGO_DB=attendance_db

# 4. Run
uvicorn app.main:app --port 8000
```
A local `.env` with the same two variables also works; real environment variables win.
Indexes are created at startup (idempotent). Check it is up: `curl localhost:8000/health`.

## Docs
- `REVIEW.md`: Part A, defects found in the starter.
- `DECISIONS.md`: Part C, design decisions and trade-offs.
