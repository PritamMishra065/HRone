"""Employee Attendance and Analytics API."""
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT = ("PRESENT", "WFH", "ON_DUTY")
STATUS = PRESENT + ("ABSENT", "LEAVE")
TIME_PATTERN = r"^(?:[01]\d|2[0-3]):[0-5]\d$"
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def db_time(value: datetime | None) -> datetime | None:
    return as_utc(value).replace(tzinfo=None, microsecond=0) if value else None


def from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def to_ms(value: datetime | None) -> int | None:
    return int(as_utc(value).timestamp() * 1000) if value else None


def parse_day(value: str, label: str = "date") -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"invalid {label}")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "month must be YYYY-MM")
    year, number = map(int, value.split("-"))
    return date(year, number, 1), date(year, number, monthrange(year, number)[1])


def overnight(employee: dict) -> bool:
    return employee["shift_end"] <= employee["shift_start"]


def shift_bounds(day_text: str, employee: dict) -> tuple[datetime, datetime]:
    day = parse_day(day_text)
    start_h, start_m = map(int, employee["shift_start"].split(":"))
    end_h, end_m = map(int, employee["shift_end"].split(":"))
    start = datetime.combine(day, time(start_h, start_m), tzinfo=IST)
    end_day = day + timedelta(days=1) if overnight(employee) else day
    end = datetime.combine(end_day, time(end_h, end_m), tzinfo=IST)
    return start.astimezone(UTC), end.astimezone(UTC)


def attendance_day(punch_in: datetime, employee: dict) -> str:
    local = punch_in.astimezone(IST)
    day = local.date()
    if overnight(employee):
        end_h, end_m = map(int, employee["shift_end"].split(":"))
        if local.time() < time(end_h, end_m):
            day -= timedelta(days=1)
    return day.isoformat()


def half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


def calculated(status: str, punch_in: datetime | None, punch_out: datetime | None,
               day_text: str, employee: dict) -> dict:
    if status not in PRESENT or punch_in is None:
        return {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    shift_start, shift_end = shift_bounds(day_text, employee)
    late_seconds = (punch_in - shift_start).total_seconds()
    late = max(0, int(late_seconds // 60)) if late_seconds > 600 else 0
    result = {"punch_in": db_time(punch_in), "punch_out": db_time(punch_out),
              "work_hours": None, "late_minutes": late,
              "overtime_minutes": 0, "half_day": False}
    if punch_out:
        hours = half_up(Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600"), 2)
        overtime = int((punch_out - shift_end).total_seconds() // 60)
        result.update(work_hours=hours, overtime_minutes=overtime if overtime >= 30 else 0,
                      half_day=hours < 4.50)
    return result


def api_value(value):
    if isinstance(value, datetime):
        return to_ms(value)
    if isinstance(value, list):
        return [api_value(item) for item in value]
    if isinstance(value, dict):
        return {key: api_value(item) for key, item in value.items()}
    return value


def employee_out(document: dict) -> dict:
    result = dict(document)
    result.pop("_id", None)
    result["created_at"] = to_ms(result["created_at"])
    return result


def attendance_out(document: dict) -> dict:
    result = dict(document)
    result.pop("_id", None)
    result.pop("version", None)
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    return api_value(result)


def request_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from:
        parse_day(date_from, "date_from")
    if date_to:
        parse_day(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not exceed date_to")
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: date

    @field_validator("shift_end")
    @classmethod
    def validate_shifts(cls, value, info):
        if value == info.data.get("shift_start"):
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: StrictInt | None = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: StrictInt | None = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    document = body.model_dump()
    document["joined_on"] = body.joined_on.isoformat()
    document["created_at"] = db_time(now_utc())
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(document)


@app.get("/employees")
def list_employees(department: str | None = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department else {}
    records = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
                   .skip((page - 1) * page_size).limit(page_size))
    return {"items": [employee_out(record) for record in records],
            "total": db.employees.count_documents(query), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day_text = attendance_day(punched_at, employee)
    document = {"emp_code": body.emp_code, "date": day_text, "status": body.status,
                **calculated(body.status, punched_at, None, day_text, employee), "history": []}
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    record = db.attendance_logs.find_one({"emp_code": body.emp_code,
                                          "punch_in": {"$lte": db_time(punched_at)}},
                                         sort=[("punch_in", DESCENDING)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    started = as_utc(record["punch_in"])
    if punched_at <= started or punched_at - started > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    update = calculated(record["status"], started, punched_at, record["date"], employee)
    record = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None}, {"$set": update},
        return_document=ReturnDocument.AFTER)
    if record is None:
        raise HTTPException(409, "record is already punched out")
    return attendance_out(record)


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None,
                    date_to: str | None = None,
                    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = request_filter(emp_code, date_from, date_to, status)
    records = list(db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                   .skip((page - 1) * page_size).limit(page_size))
    return {"items": [attendance_out(record) for record in records],
            "total": db.attendance_logs.count_documents(query), "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_day(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")
    supplied = body.model_fields_set
    if ("punch_in" in supplied and body.punch_in is None) or ("punch_out" in supplied and body.punch_out is None):
        raise HTTPException(422, "punch times must be epoch milliseconds")
    status = body.status if "status" in supplied else record["status"]
    if status in ("ABSENT", "LEAVE") and ({"punch_in", "punch_out"} & supplied):
        raise HTTPException(422, "absence statuses cannot include punch times")
    punch_in = as_utc(record["punch_in"]) if record.get("punch_in") else None
    punch_out = as_utc(record["punch_out"]) if record.get("punch_out") else None
    if "punch_in" in supplied:
        punch_in = from_ms(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in supplied:
        punch_out = from_ms(body.punch_out) if body.punch_out is not None else None
    if status in PRESENT:
        if punch_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_day(punch_in, employee) != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if punch_out and (punch_out <= punch_in or punch_out - punch_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    else:
        punch_in, punch_out = None, None
    new_values = calculated(status, punch_in, punch_out, date, employee)
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for field in fields:
        new = status if field == "status" else new_values[field]
        if record.get(field) != new:
            changes[field] = {"from": record.get(field), "to": new}
    if not changes:
        raise HTTPException(422, "request changes nothing")
    entry = {"at": db_time(now_utc()), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    version_filter = {"version": record["version"]} if "version" in record else {"version": {"$exists": False}}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], **version_filter},
        {"$set": {"status": status, **new_values}, "$push": {"history": entry}, "$inc": {"version": 1}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance record was changed concurrently")
    return attendance_out(updated)


def monthly_pipeline(emp_code: str, start: date, end: date):
    weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
    present = {"$in": ["$status", list(PRESENT)]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [weekday, present]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    summary = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end)), {})
    joined = parse_day(employee["joined_on"], "joined_on")
    first = max(start, joined)
    days = sum((first + timedelta(days=i)).weekday() < 5 for i in range((end - first).days + 1)) if first <= end else 0
    present_days = summary.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": days, "present_days": present_days,
            "leave_days": summary.get("leave_days", 0), "late_count": summary.get("late_count", 0),
            "total_late_minutes": summary.get("total_late_minutes", 0),
            "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
            "attendance_pct": half_up(Decimal(str(present_days)) * 100 / Decimal(days), 2) if days else None}


def department_pipeline(start: date, end: date, department: str | None):
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department:
        match["department"] = department
    weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [weekday, {"$in": ["$logs.status", list(PRESENT)]}]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, .5, 1]}, 0]}},
            "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]},
                {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$codes"}, "present_days": 1,
            "avg_work_hours": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    start, end = parse_month(month)
    items = list(db.employees.aggregate(department_pipeline(start, end, department)))
    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = half_up(Decimal(str(item["avg_work_hours"])), 2)
    return {"month": month, "items": items}


def leaderboard_pipeline(start: date, end: date, limit: int, department: str | None):
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    return pipeline + [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
            "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}]


@app.get("/analytics/leaderboard/late")
def leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: str | None = None):
    start, end = parse_month(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)))}


def trend_pipeline(department: str, start: date, end: date):
    count = (end - start).days + 1
    start_dt = datetime.combine(start, time.min, UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}}},
        {"$project": {"dates": {"$map": {"input": {"$range": [0, count]}, "as": "i",
            "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}},
                "format": "%Y-%m-%d", "timezone": "UTC"}}}}}},
        {"$unwind": "$dates"},
        {"$lookup": {"from": "employees", "let": {"day": "$dates"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},
            {"$count": "value"}], "as": "heads"}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$dates", "codes": "$codes"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}}], "as": "logs"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$heads.value", 0]}, 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$dates"}}}, [2, 3, 4, 5, 6]]}}},
        {"$set": {"present_count": {"$reduce": {"input": "$logs", "initialValue": 0, "in": {"$add": ["$$value",
            {"$cond": [{"$in": ["$$this.status", list(PRESENT)]}, {"$cond": [{"$ifNull": ["$$this.half_day", False]}, .5, 1]}, 0]}]}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log",
                "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$ne": ["$attendance_rate", None]},
            {"$divide": [{"$floor": {"$add": [{"$multiply": ["$attendance_rate", 10000]}, .5]}}, 10000]}, None]}}},
        {"$sort": {"dates": 1}},
        {"$setWindowFields": {"sortBy": {"dates": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
            "window": {"documents": [-6, 0]}}}}},
        {"$set": {"moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]},
            {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, .5]}}, 10000]}, None]}}},
        {"$project": {"_id": 0, "date": "$dates", "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}}]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)):
    start, end = parse_day(from_, "from"), parse_day(to, "to")
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "invalid date range")
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": list(db.employees.aggregate(trend_pipeline(department, start, end)))}


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
            emp_code: str | None = None, month: str | None = None, department: str | None = None,
            limit: int = Query(10, ge=1, le=50), date_from: str | None = None, date_to: str | None = None,
            status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None,
            from_: str | None = Query(None, alias="from"), to: str | None = None,
            page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        query = request_filter(emp_code, date_from, date_to, status)
        plan = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size).explain("executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        start, end = parse_month(month); pipeline = monthly_pipeline(emp_code, start, end); collection = "attendance_logs"
        plan = db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        start, end = parse_month(month); collection = "employees"; pipeline = department_pipeline(start, end, department)
        plan = db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        start, end = parse_month(month); collection = "attendance_logs"; pipeline = leaderboard_pipeline(start, end, limit, department)
        plan = db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    else:
        if not department or not from_ or not to: raise HTTPException(422, "department, from and to are required")
        start, end = parse_day(from_, "from"), parse_day(to, "to")
        if end < start or (end - start).days + 1 > 92: raise HTTPException(422, "invalid date range")
        collection = "employees"; pipeline = trend_pipeline(department, start, end)
        plan = db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": jsonable_encoder(plan)}
