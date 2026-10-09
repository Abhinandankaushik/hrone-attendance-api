"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded too, but real environment variables win)
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError

load_dotenv()


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"environment variable {name} is not set")
    return value


client = MongoClient(required_env("MONGO_URI"), tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[required_env("MONGO_DB")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW_HOURS = 4.5
MAX_SHIFT_LENGTH = timedelta(hours=24)
MAX_TREND_DAYS = 92
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ABSENCE_STATUSES = ["ABSENT", "LEAVE"]
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


# --------------------------------------------------------------------------- #
# Input types
# --------------------------------------------------------------------------- #
def valid_calendar_date(value: str) -> str:
    date.fromisoformat(value)
    return value


def valid_month(value: str) -> str:
    date.fromisoformat(f"{value}-01")
    return value


Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
DateStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(valid_calendar_date)]
MonthStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(valid_month)]
ShiftTime = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# The cap only keeps page * page_size inside MongoDB's 64-bit skip; any realistic page is accepted.
Page = Annotated[int, Query(ge=1, le=10**15)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_has_length(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


# Defaults are not validated, so an omitted field becomes None while an explicit null fails with 422.
class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: EpochMillis = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: EpochMillis = None


class RegularizeRequest(BaseModel):
    status: Status = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# Time and business-rule helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def from_millis(millis: int) -> datetime:
    return datetime.fromtimestamp(millis // 1000, tz=timezone.utc)


def to_millis(instant: datetime | None) -> int | None:
    if instant is None:
        return None
    return (instant - EPOCH) // timedelta(milliseconds=1)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def whole_seconds(instant: datetime) -> datetime:
    return instant.replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    hours, minutes = map(int, value.split(":"))
    return time(hours, minutes)


def round_half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def attendance_date(punch_in: datetime, emp: dict) -> date:
    local = punch_in.astimezone(IST)
    shift_start, shift_end = parse_hhmm(emp["shift_start"]), parse_hhmm(emp["shift_end"])
    # R1: on an overnight shift, an early-morning punch belongs to the shift that started the day before.
    if shift_end <= shift_start and local.time() < shift_end:
        return local.date() - timedelta(days=1)
    return local.date()


def shift_window(day: date, emp: dict) -> tuple[datetime, datetime]:
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), tzinfo=IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), tzinfo=IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, day: date, emp: dict) -> int:
    start, _ = shift_window(day, emp)
    seconds_late = int((whole_seconds(punch_in) - start).total_seconds())
    return seconds_late // 60 if seconds_late > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, day: date, emp: dict) -> int:
    _, end = shift_window(day, emp)
    minutes = int((whole_seconds(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((whole_seconds(punch_out) - whole_seconds(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / 3600, 2)


def closing_fields(punch_in: datetime, punch_out: datetime, day: date, emp: dict) -> dict:
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime_minutes(punch_out, day, emp),
        "half_day": work_hours < HALF_DAY_BELOW_HOURS,
    }


def derived_fields(status: str, punch_in: datetime | None, punch_out: datetime | None, day: date, emp: dict) -> dict:
    if status in ABSENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    fields = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        fields.update(closing_fields(punch_in, punch_out, day, emp))
    return fields


# --------------------------------------------------------------------------- #
# Errors and serialization
# --------------------------------------------------------------------------- #
def unprocessable(location: tuple, message: str) -> HTTPException:
    return HTTPException(422, [{"type": "value_error", "loc": list(location), "msg": message}])


def require_param(name: str, value):
    if value is None:
        raise unprocessable(("query", name), "Field required")
    return value


def check_punch_out(punch_in: datetime, punch_out: datetime, location: tuple) -> None:
    if punch_out <= punch_in:
        raise unprocessable(location, "punch_out must be after punch_in")
    if punch_out - punch_in > MAX_SHIFT_LENGTH:
        raise unprocessable(location, "punch_out must be within 24 hours of punch_in")


def employee_out(doc: dict) -> dict:
    return {field: doc.get(field) for field in EMPLOYEE_FIELDS} | {"created_at": to_millis(doc.get("created_at"))}


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in entry["changes"].items():
        if field in ("punch_in", "punch_out"):
            change = {"from": to_millis(change["from"]), "to": to_millis(change["to"])}
        changes[field] = change
    return {"at": to_millis(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}


def stored_values(record: dict) -> dict:
    return {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": record.get("half_day") or False,
    }


def record_out(record: dict) -> dict:
    values = stored_values(record)
    return {
        "emp_code": record["emp_code"],
        "date": record["date"],
        **values,
        "punch_in": to_millis(values["punch_in"]),
        "punch_out": to_millis(values["punch_out"]),
        "history": [history_out(entry) for entry in record.get("history") or []],
    }


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, f"employee {emp_code} not found")
    return emp


# --------------------------------------------------------------------------- #
# App, indexes, health
# --------------------------------------------------------------------------- #
EMPLOYEE_INDEXES = [
    IndexModel([("emp_code", ASCENDING)], unique=True),
    IndexModel([("department", ASCENDING), ("emp_code", ASCENDING)]),
    IndexModel([("joined_on", ASCENDING)]),
]
ATTENDANCE_INDEXES = [
    IndexModel([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True),
    IndexModel([("emp_code", ASCENDING), ("punch_in", DESCENDING)]),
    IndexModel([("date", DESCENDING), ("emp_code", ASCENDING)]),
    IndexModel([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]),
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    employees.create_indexes(EMPLOYEE_INDEXES)
    logs.create_indexes(ATTENDANCE_INDEXES)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
def database_unavailable(_: Request, __: ConnectionFailure) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except ConnectionFailure:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump() | {"created_at": utc_now()}
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"emp_code {body.emp_code} already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    total = employees.count_documents(query)
    cursor = (
        employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(emp) for emp in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee(body.emp_code)
    punched_at = utc_now() if body.punched_at is None else from_millis(body.punched_at)
    day = attendance_date(punched_at, emp)
    record = {
        "emp_code": emp["emp_code"],
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derived_fields(body.status, punched_at, None, day, emp),
        "history": [],
    }
    try:
        logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(409, f"{emp['emp_code']} already has a record for {day.isoformat()}")
    return record_out(record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee(body.emp_code)
    punched_at = utc_now() if body.punched_at is None else from_millis(body.punched_at)
    record = logs.find_one(
        {"emp_code": emp["emp_code"], "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None or record.get("punch_out") is not None:
        # An open record starting after punched_at means the punch-out is earlier than its punch-in.
        if logs.find_one({"emp_code": emp["emp_code"], "punch_in": {"$gt": punched_at}, "punch_out": None}):
            raise unprocessable(("body", "punched_at"), "punch_out must be after punch_in")
    if record is None:
        raise HTTPException(404, f"no punch-in found for {emp['emp_code']} at or before punched_at")
    if record.get("punch_out") is not None:
        raise HTTPException(409, f"record for {record['date']} is already punched out")
    check_punch_out(record["punch_in"], punched_at, ("body", "punched_at"))
    fields = closing_fields(record["punch_in"], punched_at, date.fromisoformat(record["date"]), emp)
    updated = logs.find_one_and_update(
        {"_id": record["_id"], "status": record["status"], "punch_in": record["punch_in"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, f"record for {record['date']} is already punched out")
    return record_out(updated)


def attendance_query(emp_code: str | None, date_from: str | None, date_to: str | None, status: str | None) -> dict:
    if date_from and date_to and date_from > date_to:
        raise unprocessable(("query", "date_from"), "date_from must not be after date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: DateStr | None = None,
    date_to: DateStr | None = None,
    status: Status | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_query(emp_code, date_from, date_to, status)
    total = logs.count_documents(query)
    cursor = logs.find(query, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(record) for record in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, day: Annotated[DateStr, Path(alias="date")], body: RegularizeRequest):
    emp = get_employee(emp_code)
    record = logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(404, f"no attendance record for {emp_code} on {day}")

    status = body.status or record["status"]
    if status in ABSENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(("body", "status"), f"{status} clears punch times, do not send punch_in or punch_out")
        punch_in = punch_out = None
    else:
        punch_in = record.get("punch_in") if body.punch_in is None else from_millis(body.punch_in)
        punch_out = record.get("punch_out") if body.punch_out is None else from_millis(body.punch_out)
        if punch_in is None:
            raise unprocessable(("body", "punch_in"), f"status {status} requires a punch_in")
        if body.punch_in is not None and attendance_date(punch_in, emp).isoformat() != day:
            raise unprocessable(("body", "punch_in"), f"punch_in must stay on the attendance date {day}")
        if punch_out is not None:
            check_punch_out(punch_in, punch_out, ("body", "punch_out"))

    before = stored_values(record)
    after = {"status": status, "punch_in": punch_in, "punch_out": punch_out}
    if all(before[field] == after[field] for field in after):
        raise unprocessable(("body",), "the request changes nothing")
    after |= derived_fields(status, punch_in, punch_out, date.fromisoformat(day), emp)
    changes = {field: {"from": before[field], "to": after[field]} for field in TRACKED_FIELDS if before[field] != after[field]}

    entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-set: the write only matches if nobody changed the record (or its history) since we read it.
    updated = logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "history": record.get("history"),
        },
        {"$set": after | {"history": (record.get("history") or []) + [entry]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by another request, reload and retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics pipelines
# --------------------------------------------------------------------------- #
IS_PRESENT = {"$in": ["$status", PRESENCE_STATUSES]}
IS_LATE = {"$gt": ["$late_minutes", 0]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}

MONTH_TOTALS = {
    "$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}},
        "worked_hours": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
        "worked_records": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
        "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
        "total_late_minutes": {"$sum": {"$cond": [IS_LATE, "$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
    }
}
SUMMARY_FIELDS = ("present_days", "worked_hours", "worked_records", "late_count", "total_late_minutes", "leave_count", "on_duty_count")


def half_up_expr(expression, places: int) -> dict:
    # MongoDB's $round is banker's rounding, so round half-up on exact decimals instead.
    scale = 10**places
    scaled = {"$add": [{"$multiply": [{"$toDecimal": expression}, scale]}, Decimal128("0.5")]}
    return {"$divide": [{"$floor": scaled}, scale]}


def month_range(month: str) -> tuple[date, date]:
    first = date.fromisoformat(f"{month}-01")
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def count_working_days(first: date, last: date) -> int:
    return sum(1 for offset in range((last - first).days + 1) if (first + timedelta(days=offset)).weekday() < 5)


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        MONTH_TOTALS,
    ]


def department_summary_pipeline(month: str, department: str | None) -> list:
    first, last = month_range(month)
    match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": logs.name,
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}, MONTH_TOTALS],
                "as": "totals",
            }
        },
        {"$unwind": {"path": "$totals", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{field: {"$sum": f"$totals.{field}"} for field in SUMMARY_FIELDS},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": "$headcount",
                "present_days": {"$toDouble": half_up_expr("$present_days", 2)},
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$worked_records", 0]},
                        {"$toDouble": half_up_expr({"$divide": ["$worked_hours", "$worked_records"]}, 2)},
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: str | None) -> list:
    first, last = month_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": employees.name, "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    return pipeline + [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
    ]


def department_staff(department: str) -> list[dict]:
    staff = list(employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not staff:
        raise HTTPException(404, f"department {department} not found")
    return staff


def trend_range(start: str, end: str) -> tuple[date, date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise unprocessable(("query", "to"), "to must not be before from")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(("query", "to"), f"the range must not exceed {MAX_TREND_DAYS} days")
    return first, last


def department_trend_pipeline(staff: list[dict], first: date, last: date) -> list:
    lower = datetime.combine(first, time(), tzinfo=timezone.utc)
    # $densify's upper bound is exclusive; noon of the last day includes it without overflowing past 9999-12-31.
    upper = datetime.combine(last, time(12), tzinfo=timezone.utc)
    return [
        {
            "$match": {
                "emp_code": {"$in": [emp["emp_code"] for emp in staff]},
                "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
            }
        },
        {"$group": {"_id": "$date", "present_count": {"$sum": PRESENT_VALUE}, "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {
            "$set": {
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$set": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": [emp["joined_on"] for emp in staff],
                            "as": "joined_on",
                            "cond": {"$lte": ["$$joined_on", "$date"]},
                        }
                    }
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$date",
                "is_working_day": "$is_working_day",
                "headcount": "$headcount",
                "present_count": "$present_count",
                "late_count": "$late_count",
                "attendance_rate": {"$toDouble": "$attendance_rate"},
                "moving_avg_7d": {"$toDouble": half_up_expr("$moving_avg_7d", 4)},
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = get_employee(emp_code)
    totals = next(logs.aggregate(employee_monthly_pipeline(emp_code, month)), {})
    first, last = month_range(month)
    working_days = count_working_days(max(first, date.fromisoformat(emp["joined_on"])), last)
    present_days = totals.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_count", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Decimal(present_days) * 100 / working_days, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: str | None = None):
    return {"month": month, "items": list(employees.aggregate(department_summary_pipeline(month, department)))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: Limit = 10, department: str | None = None):
    return {"month": month, "items": list(logs.aggregate(late_leaderboard_pipeline(month, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: Annotated[DateStr, Query(alias="from")],
    end: Annotated[DateStr, Query(alias="to")],
):
    first, last = trend_range(start, end)
    staff = department_staff(department)
    return {"department": department, "items": list(logs.aggregate(department_trend_pipeline(staff, first, last)))}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None,
    month: MonthStr | None = None,
    department: str | None = None,
    limit: Limit = 10,
    date_from: DateStr | None = None,
    date_to: DateStr | None = None,
    status: Status | None = None,
    start: Annotated[DateStr | None, Query(alias="from")] = None,
    end: Annotated[DateStr | None, Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection = logs
        command = {
            "find": collection.name,
            "filter": attendance_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            emp_code, month = require_param("emp_code", emp_code), require_param("month", month)
            collection, pipeline = logs, employee_monthly_pipeline(get_employee(emp_code)["emp_code"], month)
        elif endpoint == "department_summary":
            collection, pipeline = employees, department_summary_pipeline(require_param("month", month), department)
        elif endpoint == "late_leaderboard":
            collection, pipeline = logs, late_leaderboard_pipeline(require_param("month", month), limit, department)
        else:
            first, last = trend_range(require_param("from", start), require_param("to", end))
            staff = department_staff(require_param("department", department))
            collection, pipeline = logs, department_trend_pipeline(staff, first, last)
        command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}
    plan = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection.name, "explain": json.loads(json_util.dumps(plan))}
