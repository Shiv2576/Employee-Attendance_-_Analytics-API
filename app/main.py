"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from bson import Decimal128, json_util

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority

# tz_aware: PyMongo hands back UTC-aware datetimes instead of naive ones
client = MongoClient(os.environ["MONGO_URI"], tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=3000)
db = client[os.environ["MONGO_DB"]]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
DAY = timedelta(days=1)
EpochMillis = Optional[StrictInt]


def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)])
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])


@asynccontextmanager
async def lifespan(_app):
    create_indexes()  # idempotent: create_index is a no-op when the index exists
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def validation_error(loc: str, msg: str, where: str = "query"):
    """422 with FastAPI's default list-shaped body, for checks pydantic can't express."""
    return RequestValidationError([{"type": "value_error", "loc": [where, loc], "msg": msg, "input": None}])


def check_date(v: Optional[str], loc: str = "date", where: str = "query") -> Optional[str]:
    if v is None:
        return v
    try:
        if not re.match(DATE_RE, v):  # fromisoformat alone also takes "2026-W01-1" and "20260706"
            raise ValueError
        date.fromisoformat(v)
    except ValueError:
        raise validation_error(loc, "must be a valid YYYY-MM-DD date", where)
    return v


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    first = date.fromisoformat(month + "-01")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first.isoformat(), (nxt - DAY).isoformat()


def weekdays_between(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] (R7). Calendar arithmetic only, no data access."""
    if start > end:
        return 0
    days = (end - start).days + 1
    full, rest = divmod(days, 7)
    return full * 5 + sum(1 for i in range(rest) if (start + timedelta(days=i)).weekday() < 5)


def half_up(x, places: int) -> Optional[float]:
    """R8: half-up rounding through Decimal (accepts float, int, Decimal or bson Decimal128)."""
    if x is None:
        return None
    if hasattr(x, "to_decimal"):
        x = x.to_decimal()
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # BSON dates are UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def hhmm(s: str) -> tuple[int, int]:
    h, m = map(int, s.split(":"))
    return h, m


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST date of punch_in; on overnight shifts a punch before shift_end belongs to the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.strftime("%H:%M:%S") < shift_end + ":00":
        d -= timedelta(days=1)
    return d.isoformat()


def shift_bound(date_str: str, at: str, next_day: bool = False) -> datetime:
    h, m = hhmm(at)
    return datetime.combine(date.fromisoformat(date_str), datetime.min.time(), IST).replace(hour=h, minute=m) + timedelta(
        days=1 if next_day else 0
    )


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then floor minutes since shift_start."""
    seconds = int((punch_in - shift_bound(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (round() is banker's rounding on a float)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: floor minutes past shift_end, only when >= 30. Overnight shifts end on the next day."""
    end = shift_bound(date_str, shift_end, next_day=is_overnight(shift_start, shift_end))
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, date_str: str) -> dict:
    """Punch times + every derived field for a record's final state (R2-R5, DATA_MODEL "Situation" table)."""
    if status not in PRESENCE:
        return {"punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                "half_day": False}
    out = {"punch_in": punch_in, "punch_out": punch_out, "work_hours": None,
           "late_minutes": compute_late_minutes(punch_in, emp["shift_start"], date_str),
           "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=wh, half_day=wh < 4.5,
                   overtime_minutes=compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], date_str))
    return out


def out_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc.get("created_at"))
    return doc


def out_record(doc: dict) -> dict:
    """Stored attendance doc -> API shape: no _id, instants as epoch ms, legacy gaps filled."""
    history = []
    for h in doc.get("history") or []:
        changes = {}
        for field, ch in (h.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                ch = {"from": dt_to_ms(ch.get("from")), "to": dt_to_ms(ch.get("to"))}
            changes[field] = ch
        history.append({"at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": doc.get("half_day") or False,
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v):
        date.fromisoformat(v)  # ValueError -> 422 (rejects 2026-02-30)
        return v

    @model_validator(mode="after")
    def _distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = Field(None, ge=MIN_MS, le=MAX_MS)
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = Field(None, ge=MIN_MS, le=MAX_MS)


class RegularizeIn(BaseModel):
    """Derived fields are not declared, so a client that sends them is ignored (pydantic drops extra keys)."""
    status: Optional[Status] = None
    punch_in: EpochMillis = Field(None, ge=MIN_MS, le=MAX_MS)
    punch_out: EpochMillis = Field(None, ge=MIN_MS, le=MAX_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        for f in ("status", "punch_in", "punch_out"):  # the contract types are not nullable; omit a field to keep it
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} may be omitted but not null")
        return self


# --------------------------------------------------------------------------- #
# Query and pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_query(emp_code, date_from, date_to, status) -> dict:
    check_date(date_from, "date_from")
    check_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise validation_error("date_from", "date_from must be on or before date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def _f(prefix: str, name: str) -> str:
    return f"${prefix}{name}"


def present_value(p: str = "") -> dict:
    """R5/R6: 1 for a presence-status record, 0.5 when it is a half day, 0 otherwise."""
    return {"$cond": [{"$in": [{"$ifNull": [_f(p, "status"), None]}, PRESENCE]},
                      {"$cond": [{"$eq": [{"$ifNull": [_f(p, "half_day"), False]}, True]}, 0.5, 1]}, 0]}


def is_weekday(p: str = "") -> dict:
    """R7: Mon-Fri, from the stored YYYY-MM-DD string (a calendar date, so no time zone is involved)."""
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": _f(p, "date"), "format": "%Y-%m-%d"}}}, 5]}


def late_of(p: str = "") -> dict:
    return {"$ifNull": [_f(p, "late_minutes"), 0]}


def count_if(cond) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [is_weekday(), present_value(), 0]}},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if({"$gt": [late_of(), 0]}),
            "total_late_minutes": {"$sum": late_of()},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    """Runs on employees, so people with zero logs still produce one row (R9); logs are joined per employee."""
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    g = "logs."
    wh = _f(g, "work_hours")
    has_hours = {"$and": [{"$in": [{"$ifNull": [_f(g, "status"), None]}, PRESENCE]}, {"$isNumber": wh}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        # an employee with no logs survives as one document whose log index is null
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True, "includeArrayIndex": "i"}},
        {"$group": {
            "_id": "$department",
            "headcount": count_if({"$lte": [{"$ifNull": ["$i", 0]}, 0]}),
            "present_days": {"$sum": {"$cond": [{"$and": [{"$isNumber": "$i"}, is_weekday(g)]}, present_value(g), 0]}},
            # summed in Decimal128 so the mean is exact before half-up rounding (a double sum drifts off the ties)
            "wh_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": wh}, 0]}},
            "wh_n": count_if(has_hours),
            "late_count": count_if({"$gt": [late_of(g), 0]}),
            "total_late_minutes": {"$sum": late_of(g)},
            "leave_count": count_if({"$eq": [_f(g, "status"), "LEAVE"]}),
            "on_duty_count": count_if({"$eq": [_f(g, "status"), "ON_DUTY"]}),
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}], "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees drop out here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank is standard competition ranking (1, 2, 2, 4); limit filters on rank, not on row count
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def round_half_up_expr(x, places: int) -> dict:
    """Half-up rounding in Decimal128 (MongoDB's $round is half-to-even, and doubles can't hold the ties exactly)."""
    scale = Decimal128(str(10 ** places))
    return {"$let": {"vars": {"v": x}, "in": {"$cond": [
        {"$eq": [{"$ifNull": ["$$v", None]}, None]}, None,
        {"$divide": [{"$floor": {"$add": [{"$multiply": ["$$v", scale]}, Decimal128("0.5")]}}, scale]},
    ]}}}


def trend_pipeline(department: str, frm: str, to: str) -> list:
    """One row per day in [frm, to]. Each employee emits headcount/presence events, $densify fills the calendar,
    and window functions give the running headcount (R9) and the 7-row moving average."""
    start = datetime.fromisoformat(frm).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(to).replace(tzinfo=timezone.utc) + DAY  # $densify upper bound is exclusive
    # "$g." prefix -> "$$g.<field>": the same expressions, applied to each joined log inside $map
    log_event = {"date": "$$g.date", "hc": 0, "p": present_value("$g."),
                 "l": {"$cond": [{"$gt": [late_of("$g."), 0]}, 1, 0]}}
    rate = {"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": frm, "$lte": to}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            # every employee contributes a row on `frm`, so the range is never empty; it carries headcount when
            # they had already joined, and a later join inside the range adds one on joined_on
            [{"date": frm, "hc": {"$cond": [{"$lte": ["$joined_on", frm]}, 1, 0]}, "p": 0, "l": 0}],
            {"$cond": [{"$and": [{"$gt": ["$joined_on", frm]}, {"$lte": ["$joined_on", to]}]},
                       [{"date": "$joined_on", "hc": 1, "p": 0, "l": 0}], []]},
            {"$map": {"input": "$logs", "as": "g", "in": log_event}},
        ]}}},
        {"$unwind": "$events"},
        {"$group": {"_id": "$events.date", "hc": {"$sum": "$events.hc"}, "p": {"$sum": "$events.p"},
                    "l": {"$sum": "$events.l"}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"headcount": {
            "$sum": {"$ifNull": ["$hc", 0]}, "window": {"documents": ["unbounded", "current"]}}}}},
        {"$addFields": {"present_count": {"$ifNull": ["$p", 0]}, "late_count": {"$ifNull": ["$l", 0]},
                        "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, round_half_up_expr(rate, 4), None]}}},
        # $avg skips nulls and returns null when the window has none; the window never reaches before `frm`
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {
            "$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                      "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                      "attendance_rate": 1, "moving_avg_7d": round_half_up_expr("$moving_avg_7d", 4)}},
    ]


def trend_range(frm: str, to: str):
    check_date(frm, "from")
    check_date(to, "to")
    if to < frm:
        raise validation_error("to", "to must be on or after from")
    if (date.fromisoformat(to) - date.fromisoformat(frm)).days + 1 > 92:
        raise validation_error("to", "the range may not exceed 92 days")


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        return JSONResponse({"detail": "database unavailable"}, status_code=503)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index makes concurrent duplicates a 409, not a second row
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return out_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [out_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return out_record(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [out_record(d) for d in cur], "total": total, "page": page, "page_size": page_size}


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # most recent record punched in at or before ts; one record per date, so date order is punch_in order.
    # Searching by instant (not by today's date) is what makes an overnight shift close across midnight.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", DESCENDING)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts - pin > timedelta(hours=24):
        raise validation_error("punched_at", "must be after punch_in and within 24 hours of it", "body")
    d = derive(rec["status"], pin, ts, emp, rec["date"])
    # conditional update: of two simultaneous punch-outs only one still sees punch_out == null
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": pin},
        {"$set": {k: d[k] for k in ("punch_out", "work_hours", "overtime_minutes", "half_day")}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return out_record(updated)


TRACKED = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeIn, emp_code: str, day: str = Path(alias="date")):
    check_date(day, "date", "path")
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not rec:
        raise HTTPException(404, "no attendance record for that date")

    def bad(msg, loc="body"):
        return validation_error(loc, msg, "body")

    old = {
        "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": rec.get("half_day") or False,
    }
    status = body.status or old["status"]
    if status not in PRESENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise bad(f"{status} clears the punch times; punch_in/punch_out can not be supplied", "status")
        pin = pout = None
    else:
        pin = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        if pin is None:
            raise bad("a presence status requires a punch_in", "punch_in")
        if body.punch_in is not None and attendance_date(pin, emp["shift_start"], emp["shift_end"]) != day:
            raise bad("punch_in must fall on the record's attendance date (R1)", "punch_in")
        pout = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            raise bad("punch_out must be after punch_in and within 24 hours of it", "punch_out")
    new = {"status": status, **derive(status, pin, pout, emp, day)}
    if all(new[f] == old[f] for f in ("status", "punch_in", "punch_out")):
        raise bad("the request changes nothing")
    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED if new[f] != old[f]}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: apply only if the record still holds what we read (same times, same history
    # length). A concurrent correction makes this match nothing -> 409, so no history entry is ever lost.
    # Pipeline update so a legacy record without `history` still gets an array; $literal keeps user text such
    # as "$5 refund" from being read as a field path.
    flt = {"_id": rec["_id"], "status": rec["status"], "punch_in": old["punch_in"], "punch_out": old["punch_out"],
           "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]}}
    sets = {f: {"$literal": new[f]} for f in TRACKED}
    sets["history"] = {"$concatArrays": [{"$ifNull": ["$history", []]}, [{"$literal": entry}]]}
    updated = db.attendance_logs.find_one_and_update(flt, [{"$set": sets}], return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was changed by another request; retry")
    return out_record(updated)


# --------------------------------------------------------------------------- #
# Analytics (MongoDB aggregations; Python only rounds the final numbers, R8)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    row = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)), {})
    start = max(date.fromisoformat(first), date.fromisoformat(emp["joined_on"]))
    working = weekdays_between(start, date.fromisoformat(last))
    present = Decimal(str(row.get("present_days", 0)))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": half_up(present, 2),
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(present / working * 100, 2) if working else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(summary_pipeline(first, last, department)):
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": half_up(r["present_days"], 2),
            "avg_work_hours": half_up(r["wh_sum"].to_decimal() / r["wh_n"], 2) if r["wh_n"] else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_RE), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None
):
    first, last = month_bounds(month)
    keys = ("rank", "emp_code", "name", "department", "total_late_minutes", "late_count")
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department))
    return {"month": month, "items": [{k: r[k] for k in keys} for r in rows]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, frm: str = Query(alias="from"), to: str = Query()):
    trend_range(frm, to)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "unknown department")
    items = [
        {
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": half_up(r["present_count"], 2),
            "late_count": r["late_count"],
            "attendance_rate": half_up(r["attendance_rate"], 4),
            "moving_avg_7d": half_up(r["moving_avg_7d"], 4),
        }
        for r in db.employees.aggregate(trend_pipeline(department, frm, to))
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """explain(executionStats) of the endpoint's main query, built by the same builder the endpoint uses."""

    def need(**params):
        for k, v in params.items():
            if v is None:
                raise validation_error(k, "field required")

    if endpoint == "attendance_list":
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": attendance_query(emp_code, date_from, date_to, status),
               "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            coll, pipeline = "attendance_logs", monthly_pipeline(emp_code, *month_bounds(month))
        elif endpoint == "department_summary":
            need(month=month)
            coll, pipeline = "employees", summary_pipeline(*month_bounds(month), department)
        elif endpoint == "late_leaderboard":
            need(month=month)
            coll, pipeline = "attendance_logs", leaderboard_pipeline(*month_bounds(month), limit, department)
        else:
            need(department=department, **{"from": frm, "to": to})
            trend_range(frm, to)
            coll, pipeline = "employees", trend_pipeline(department, frm, to)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    out = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(out))}
