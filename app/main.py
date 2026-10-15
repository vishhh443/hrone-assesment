"""Employee attendance and analytics API backed by MongoDB Atlas."""

import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

logger = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MILLIS = 100_000_000_000
MAX_EPOCH_MILLIS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

client = None
db = None
_indexes_ready = False
_configuration_error: str | None = None


def _validate_date_string(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Date must be a valid YYYY-MM-DD calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("Date must use YYYY-MM-DD format")
    return value


def _validate_month_string(value: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Month must use YYYY-MM format")
    return value


CalendarDate = Annotated[str, AfterValidator(_validate_date_string)]
MonthString = Annotated[str, AfterValidator(_validate_month_string)]
EpochMillis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MILLIS, le=MAX_EPOCH_MILLIS)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=1, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: CalendarDate

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis | None = None
    status: PresenceStatus = "PRESENT"

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis | None = None

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: AttendanceStatus | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_null_change_values(self):
        for field_name in ("status", "punch_in", "punch_out"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _truncate_seconds(value: datetime) -> datetime:
    return _as_utc(value).replace(microsecond=0)


def _from_epoch_millis(value: int | None) -> datetime:
    if value is None:
        return _truncate_seconds(datetime.now(UTC))
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def _to_epoch_millis(value: datetime | None) -> int | None:
    if value is None:
        return None
    delta = _as_utc(value) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _month_bounds(month: str) -> tuple[str, str]:
    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def _shift_start_instant(attendance_date: str, shift_start: str) -> datetime:
    local_date = date.fromisoformat(attendance_date)
    local_time = time.fromisoformat(shift_start)
    return datetime.combine(local_date, local_time, tzinfo=IST).astimezone(UTC)


def _shift_end_instant(
    attendance_date: str, shift_start: str, shift_end: str
) -> datetime:
    local_date = date.fromisoformat(attendance_date)
    if shift_end <= shift_start:
        local_date += timedelta(days=1)
    return datetime.combine(local_date, time.fromisoformat(shift_end), tzinfo=IST).astimezone(UTC)


def _attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local_punch = _as_utc(punch_in).astimezone(IST)
    day = local_punch.date()
    if shift_end <= shift_start and local_punch.time().replace(tzinfo=None) < time.fromisoformat(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(
    punch_in: datetime, shift_start: str, attendance_date: str | None = None
) -> int:
    punch_in = _truncate_seconds(punch_in)
    if attendance_date is None:
        attendance_date = punch_in.astimezone(IST).date().isoformat()
    shift_start_at = _shift_start_instant(attendance_date, shift_start)
    seconds_late = int((punch_in - shift_start_at).total_seconds())
    return seconds_late // 60 if seconds_late > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((_truncate_seconds(punch_out) - _truncate_seconds(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str = "09:30",
) -> int:
    end_at = _shift_end_instant(date_str, shift_start, shift_end)
    seconds_after_shift = int((_truncate_seconds(punch_out) - end_at).total_seconds())
    if seconds_after_shift < 1_800:
        return 0
    return seconds_after_shift // 60


def _computed_fields(
    status: str,
    punch_in: datetime | None,
    punch_out: datetime | None,
    employee: dict,
    attendance_date: str,
) -> dict:
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    punch_in = _truncate_seconds(punch_in)
    late_minutes = compute_late_minutes(punch_in, employee["shift_start"], attendance_date)
    if punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
        }
    punch_out = _truncate_seconds(punch_out)
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": compute_overtime(
            punch_out,
            employee["shift_end"],
            attendance_date,
            shift_start=employee["shift_start"],
        ),
        "half_day": work_hours < 4.50,
    }


def _serialize_employee(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    result["created_at"] = _to_epoch_millis(result["created_at"])
    return result


def _serialize_attendance(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    result["punch_in"] = _to_epoch_millis(result.get("punch_in"))
    result["punch_out"] = _to_epoch_millis(result.get("punch_out"))
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    history = result.get("history") or []
    for entry in history:
        entry["at"] = _to_epoch_millis(entry.get("at"))
        changes = entry.get("changes") or {}
        for field_name in ("punch_in", "punch_out"):
            if field_name in changes:
                changes[field_name]["from"] = _to_epoch_millis(changes[field_name].get("from"))
                changes[field_name]["to"] = _to_epoch_millis(changes[field_name].get("to"))
    result["history"] = history
    return result


def _create_indexes(database) -> None:
    database.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code"
    )
    database.employees.create_index(
        [("joined_on", ASCENDING)], name="ix_employees_joined_on"
    )
    database.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employees_department_joined",
    )
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_date",
    )
    database.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp",
    )
    database.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_late_emp",
    )


def _initialize_database() -> None:
    global _indexes_ready, _configuration_error
    if client is None or db is None:
        raise RuntimeError(_configuration_error or "MongoDB is not configured")
    client.admin.command("ping")
    if not _indexes_ready:
        try:
            _create_indexes(db)
        except DuplicateKeyError:
            _configuration_error = (
                "Existing duplicate emp_code or (emp_code, date) values prevent required unique indexes"
            )
            logger.error(
                "Required unique index creation failed because existing employee or attendance "
                "records contain duplicate keys; no records were deleted"
            )
            raise
        _indexes_ready = True
    _configuration_error = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global client, db, _indexes_ready, _configuration_error
    uri = os.getenv("MONGO_URI")
    database_name = os.getenv("MONGO_DB", "attendance_db")
    _indexes_ready = False
    if not uri:
        client = None
        db = None
        _configuration_error = "MONGO_URI is not configured"
        logger.error("MONGO_URI is not configured; MongoDB endpoints are unavailable")
    else:
        try:
            client = MongoClient(
                uri,
                tls=True,
                tz_aware=True,
                connectTimeoutMS=5_000,
                serverSelectionTimeoutMS=5_000,
                socketTimeoutMS=10_000,
            )
            db = client[database_name]
            _configuration_error = None
            _initialize_database()
        except PyMongoError as exc:
            _configuration_error = "MongoDB connection or index initialization failed"
            logger.error(
                "MongoDB initialization failed (%s); verify URI, Atlas network access, TLS, "
                "and required index compatibility",
                type(exc).__name__,
            )
    try:
        yield
    finally:
        if client is not None:
            client.close()
        client = None
        db = None
        _indexes_ready = False


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_request, exc: PyMongoError):
    logger.error("MongoDB operation failed (%s)", type(exc).__name__)
    return JSONResponse(
        status_code=503, content={"detail": "MongoDB is unavailable"}
    )


def _require_db():
    if db is None or not _indexes_ready:
        raise HTTPException(503, "MongoDB is not configured or ready")
    return db


def _attendance_filter(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_filter = {}
        if date_from is not None:
            date_filter["$gte"] = date_from
        if date_to is not None:
            date_filter["$lte"] = date_to
        query["date"] = date_filter
    if status is not None:
        query["status"] = status
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    return query


def _attendance_query_hint(emp_code: str | None) -> str:
    return "uq_attendance_emp_date" if emp_code is not None else "ix_attendance_date_emp"


def _weekday_expr(date_field: str) -> dict:
    day_of_week = {"$dayOfWeek": {"$dateFromString": {
        "dateString": date_field,
        "format": "%Y-%m-%d",
        "timezone": "Asia/Kolkata",
    }}}
    return {"$and": [{"$gte": [day_of_week, 2]}, {"$lte": [day_of_week, 6]}]}


def _working_days_expr(month_start: str, month_end: str) -> dict:
    joined = {
        "$dateFromString": {
            "dateString": {
                "$cond": [{"$gt": ["$joined_on", month_start]}, "$joined_on", month_start]
            },
            "format": "%Y-%m-%d",
            "timezone": "Asia/Kolkata",
        }
    }
    end = {
        "$dateFromString": {
            "dateString": month_end,
            "format": "%Y-%m-%d",
            "timezone": "Asia/Kolkata",
        }
    }
    offsets = {
        "$range": [
            0,
            {
                "$max": [
                    0,
                    {
                        "$add": [
                            {
                                "$dateDiff": {
                                    "startDate": joined,
                                    "endDate": end,
                                    "unit": "day",
                                    "timezone": "Asia/Kolkata",
                                }
                            },
                            1,
                        ]
                    },
                ]
            },
        ]
    }
    day_of_week = {
        "$dayOfWeek": {
            "$dateAdd": {
                "startDate": joined,
                "unit": "day",
                "amount": "$$offset",
                "timezone": "Asia/Kolkata",
            }
        }
    }
    return {
        "$size": {
            "$filter": {
                "input": {"$map": {"input": offsets, "as": "offset", "in": day_of_week}},
                "as": "weekday",
                "cond": {"$and": [{"$gte": ["$$weekday", 2]}, {"$lte": ["$$weekday", 6]}]},
            }
        }
    }


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    month_start, month_end = _month_bounds(month)
    attendance_pipeline = [
        {
            "$match": {
                "$expr": {
                    "$and": [
                        {"$eq": ["$emp_code", "$$emp_code"]},
                        {"$gte": ["$date", month_start]},
                        {"$lte": ["$date", month_end]},
                        {"$gte": ["$date", "$$joined_on"]},
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    _weekday_expr("$date"),
                                ]
                            },
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code", "joined_on": "$joined_on"},
                "pipeline": attendance_pipeline,
                "as": "attendance",
            }
        },
        {"$set": {"attendance": {"$ifNull": [{"$arrayElemAt": ["$attendance", 0]}, {}]}}},
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": _working_days_expr(month_start, month_end),
                "present_days": {"$ifNull": ["$attendance.present_days", 0]},
                "leave_days": {"$ifNull": ["$attendance.leave_days", 0]},
                "late_count": {"$ifNull": ["$attendance.late_count", 0]},
                "total_late_minutes": {"$ifNull": ["$attendance.total_late_minutes", 0]},
                "total_overtime_minutes": {"$ifNull": ["$attendance.total_overtime_minutes", 0]},
            }
        },
    ]


def _department_summary_pipeline(month: str, department: str | None) -> list[dict]:
    month_start, month_end = _month_bounds(month)
    employee_match: dict = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    employee_metrics = [
        {
            "$match": {
                "$expr": {
                    "$and": [
                        {"$eq": ["$emp_code", "$$emp_code"]},
                        {"$gte": ["$date", month_start]},
                        {"$lte": ["$date", month_end]},
                        {"$gte": ["$date", "$$joined_on"]},
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    _weekday_expr("$date"),
                                ]
                            },
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code", "joined_on": "$joined_on"},
                "pipeline": employee_metrics,
                "as": "attendance",
            }
        },
        {"$set": {"attendance": {"$ifNull": [{"$arrayElemAt": ["$attendance", 0]}, {}]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$attendance.present_days", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$attendance.late_count", 0]}},
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$attendance.total_late_minutes", 0]}
                },
                "leave_count": {"$sum": {"$ifNull": ["$attendance.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$attendance.on_duty_count", 0]}},
                "work_hours_sum": {"$sum": {"$ifNull": ["$attendance.work_hours_sum", 0]}},
                "work_hours_count": {"$sum": {"$ifNull": ["$attendance.work_hours_count", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
            }
        },
    ]


def _late_leaderboard_pipeline(month: str, limit: int, department: str | None) -> list[dict]:
    month_start, month_end = _month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def _round_half_up(value: float | int | None, places: int = 2) -> float | None:
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict]:
    # Expand employee documents to calendar days so empty dates exist before the daily group.
    from_instant = {
        "$dateFromString": {
            "dateString": from_date,
            "format": "%Y-%m-%d",
            "timezone": "Asia/Kolkata",
        }
    }
    to_instant = {
        "$dateFromString": {
            "dateString": to_date,
            "format": "%Y-%m-%d",
            "timezone": "Asia/Kolkata",
        }
    }
    day_count = {
        "$add": [
            {
                "$dateDiff": {
                    "startDate": from_instant,
                    "endDate": to_instant,
                    "unit": "day",
                    "timezone": "Asia/Kolkata",
                }
            },
            1,
        ]
    }
    generated_days = {
        "$map": {
            "input": {"$range": [0, day_count]},
            "as": "offset",
            "in": {
                "$dateAdd": {
                    "startDate": from_instant,
                    "unit": "day",
                    "amount": "$$offset",
                    "timezone": "Asia/Kolkata",
                }
            },
        }
    }
    return [
        {"$match": {"department": department}},
        {"$set": {"_trend_dates": generated_days}},
        {"$unwind": "$_trend_dates"},
        {
            "$set": {
                "_trend_day": {
                    "$dateToString": {
                        "date": "$_trend_dates",
                        "format": "%Y-%m-%d",
                        "timezone": "Asia/Kolkata",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code", "day": "$_trend_day"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp_code"]},
                                    {"$eq": ["$date", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "_day_attendance",
            }
        },
        {"$set": {"_day_attendance": {"$arrayElemAt": ["$_day_attendance", 0]}}},
        {
            "$group": {
                "_id": "$_trend_dates",
                "headcount": {
                    "$sum": {"$cond": [{"$lte": ["$joined_on", "$_trend_day"]}, 1, 0]}
                },
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$lte": ["$joined_on", "$_trend_day"]},
                                    {"$in": ["$_day_attendance.status", list(PRESENCE_STATUSES)]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$_day_attendance.half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$lte": ["$joined_on", "$_trend_day"]},
                                    {"$gt": [{"$ifNull": ["$_day_attendance.late_minutes", 0]}, 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": {"date": "$_id", "timezone": "Asia/Kolkata"}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"date": "$_id", "timezone": "Asia/Kolkata"}}, 6]},
                    ]
                },
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {
                                    "$gte": [
                                        {"$dayOfWeek": {"date": "$_id", "timezone": "Asia/Kolkata"}},
                                        2,
                                    ]
                                },
                                {
                                    "$lte": [
                                        {"$dayOfWeek": {"date": "$_id", "timezone": "Asia/Kolkata"}},
                                        6,
                                    ]
                                },
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        {
                            "$divide": [
                                {"$trunc": {"$add": [{"$multiply": ["$attendance_rate", 10_000]}, 0.5]}},
                                10_000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {
                            "$divide": [
                                {"$trunc": {"$add": [{"$multiply": ["$moving_avg_7d", 10_000]}, 0.5]}},
                                10_000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "date": "$_id",
                        "format": "%Y-%m-%d",
                        "timezone": "Asia/Kolkata",
                    }
                },
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/health")
def health():
    try:
        _initialize_database()
    except RuntimeError:
        raise HTTPException(503, "MongoDB is not configured or ready")
    except PyMongoError as exc:
        logger.error("MongoDB health check failed (%s)", type(exc).__name__)
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    database = _require_db()
    document = body.model_dump()
    now = datetime.now(UTC)
    document["created_at"] = now.replace(microsecond=(now.microsecond // 1_000) * 1_000)
    try:
        database.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return _serialize_employee(document)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    database = _require_db()
    query = {"department": department} if department is not None else {}
    total = database.employees.count_documents(query)
    documents = database.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    documents = documents.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [_serialize_employee(document) for document in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    database = _require_db()
    employee = database.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    punched_at = _from_epoch_millis(body.punched_at)
    attendance_date = _attendance_date(punched_at, employee["shift_start"], employee["shift_end"])
    document = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at, employee["shift_start"], attendance_date
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        database.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return _serialize_attendance(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    database = _require_db()
    employee = database.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    punched_at = _from_epoch_millis(body.punched_at)
    open_filter = {
        "emp_code": body.emp_code,
        "status": {"$in": list(PRESENCE_STATUSES)},
        "punch_in": {"$lte": punched_at},
        "punch_out": None,
    }
    record = database.attendance_logs.find_one(
        open_filter, sort=[("punch_in", DESCENDING)]
    )
    if record is None:
        latest_before = database.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "status": {"$in": list(PRESENCE_STATUSES)},
                "punch_in": {"$lte": punched_at},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if latest_before is not None and latest_before.get("punch_out") is not None:
            raise HTTPException(409, "attendance record is already punched out")
        if latest_before is None:
            future = database.attendance_logs.find_one(
                {
                    "emp_code": body.emp_code,
                    "status": {"$in": list(PRESENCE_STATUSES)},
                    "punch_in": {"$gt": punched_at},
                },
                sort=[("punch_in", ASCENDING)],
            )
            if future is not None:
                raise HTTPException(422, "punch-out must be after punch-in")
        raise HTTPException(404, "No eligible punch-in found")
    punch_in_at = _truncate_seconds(record["punch_in"])
    if punched_at <= punch_in_at:
        raise HTTPException(422, "punch-out must be after punch-in")
    if punched_at - punch_in_at > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be within 24 hours of punch-in")
    computed = _computed_fields(
        record["status"], punch_in_at, punched_at, employee, record["date"]
    )
    updated = database.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **computed}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return _serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: CalendarDate | None = None,
    date_to: CalendarDate | None = None,
    status: AttendanceStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    database = _require_db()
    query = _attendance_filter(emp_code, date_from, date_to, status)
    total = database.attendance_logs.count_documents(query)
    hint = _attendance_query_hint(emp_code)
    documents = (
        database.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
        .hint(hint)
    )
    return {
        "items": [_serialize_attendance(document) for document in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: CalendarDate,
    body: RegularizeRequest,
):
    attendance_date = date
    database = _require_db()
    employee = database.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    record = database.attendance_logs.find_one(
        {"emp_code": emp_code, "date": attendance_date}
    )
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    supplied = body.model_fields_set
    status = body.status if "status" in supplied else record["status"]
    if status in ("ABSENT", "LEAVE") and ({"punch_in", "punch_out"} & supplied):
        raise HTTPException(422, "ABSENT and LEAVE records cannot include punch times")
    punch_in_at = (
        _from_epoch_millis(body.punch_in)
        if "punch_in" in supplied and body.punch_in is not None
        else record.get("punch_in")
        if "punch_in" not in supplied
        else None
    )
    punch_out_at = (
        _from_epoch_millis(body.punch_out)
        if "punch_out" in supplied and body.punch_out is not None
        else record.get("punch_out")
        if "punch_out" not in supplied
        else None
    )
    if status in ("ABSENT", "LEAVE"):
        punch_in_at = None
        punch_out_at = None
    elif punch_in_at is None:
        raise HTTPException(422, "A presence status requires punch_in")
    else:
        punch_in_at = _truncate_seconds(punch_in_at)
        if _attendance_date(
            punch_in_at, employee["shift_start"], employee["shift_end"]
        ) != attendance_date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
    if punch_out_at is not None:
        punch_out_at = _truncate_seconds(punch_out_at)
        if punch_in_at is None or punch_out_at <= _truncate_seconds(punch_in_at):
            raise HTTPException(422, "punch_out must be after punch_in")
        if punch_out_at - _truncate_seconds(punch_in_at) > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    computed = _computed_fields(status, punch_in_at, punch_out_at, employee, attendance_date)
    old_values = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_values = {
        "status": status,
        "punch_in": punch_in_at,
        "punch_out": punch_out_at,
        **computed,
    }
    changes = {}
    for field_name, new_value in new_values.items():
        old_value = old_values[field_name]
        if field_name in ("punch_in", "punch_out"):
            if old_value is not None:
                old_value = _as_utc(old_value)
        if old_value != new_value:
            changes[field_name] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "The request does not change the attendance record")

    now = datetime.now(UTC)
    audit_entry = {
        "at": now.replace(microsecond=(now.microsecond // 1_000) * 1_000),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history_filter = (
        {"history": record["history"]}
        if "history" in record
        else {"history": {"$exists": False}}
    )
    updated = database.attendance_logs.find_one_and_update(
        {"_id": record["_id"], **history_filter},
        {
            "$set": {
                "status": status,
                "punch_in": punch_in_at,
                "punch_out": punch_out_at,
                **computed,
            },
            "$push": {"history": audit_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Attendance record changed concurrently; retry the correction")
    return _serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthString):
    database = _require_db()
    if database.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "Employee not found")
    result = next(
        database.employees.aggregate(
            _employee_monthly_pipeline(emp_code, month),
            hint="uq_employees_emp_code",
        ),
        None,
    )
    if result is None:
        raise HTTPException(404, "Employee not found")
    working_days = result["working_days"]
    present_days = _round_half_up(result["present_days"])
    attendance_pct = (
        _round_half_up(present_days / working_days * 100)
        if working_days > 0
        else None
    )
    result["present_days"] = present_days
    result["attendance_pct"] = attendance_pct
    return result


@app.get("/analytics/departments/summary")
def department_summary(month: MonthString, department: str | None = None):
    database = _require_db()
    pipeline = _department_summary_pipeline(month, department)
    hint = (
        "ix_employees_department_joined"
        if department is not None
        else "ix_employees_joined_on"
    )
    items = list(database.employees.aggregate(pipeline, hint=hint))
    for item in items:
        item["present_days"] = _round_half_up(item["present_days"])
        item["avg_work_hours"] = _round_half_up(item["avg_work_hours"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthString,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
):
    database = _require_db()
    pipeline = _late_leaderboard_pipeline(month, limit, department)
    items = list(
        database.attendance_logs.aggregate(
            pipeline,
            hint="ix_attendance_date_late_emp",
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[CalendarDate, Query(alias="from")],
    to_date: Annotated[CalendarDate, Query(alias="to")],
):
    database = _require_db()
    if to_date < from_date:
        raise HTTPException(422, "to must not be before from")
    inclusive_days = (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1
    if inclusive_days > 92:
        raise HTTPException(422, "Date range must not exceed 92 days")
    if database.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "Department not found")
    items = list(
        database.employees.aggregate(
            _trend_pipeline(department, from_date, to_date),
            hint="ix_employees_department_joined",
        )
    )
    return {"department": department, "items": items}


def _explain_aggregation(database, collection_name: str, pipeline: list[dict], hint: str):
    command = {
        "aggregate": collection_name,
        "pipeline": pipeline,
        "cursor": {},
        "hint": hint,
    }
    return database.command("explain", command, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: str | None = None,
    month: MonthString | None = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: CalendarDate | None = None,
    date_to: CalendarDate | None = None,
    status: AttendanceStatus | None = None,
    from_date: Annotated[CalendarDate | None, Query(alias="from")] = None,
    to_date: Annotated[CalendarDate | None, Query(alias="to")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    database = _require_db()
    if endpoint == "attendance_list":
        query = _attendance_filter(emp_code, date_from, date_to, status)
        hint = _attendance_query_hint(emp_code)
        explain = database.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "projection": {"_id": 0},
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": (page - 1) * page_size,
                "limit": page_size,
                "hint": hint,
            },
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            if month is None:
                raise HTTPException(422, "month is required for employee_monthly")
            if emp_code is None:
                raise HTTPException(422, "emp_code is required for employee_monthly")
            pipeline = _employee_monthly_pipeline(emp_code, month)
            hint = "uq_employees_emp_code"
            collection_name = "employees"
        elif endpoint == "department_summary":
            if month is None:
                raise HTTPException(422, "month is required for department_summary")
            pipeline = _department_summary_pipeline(month, department)
            hint = (
                "ix_employees_department_joined"
                if department is not None
                else "ix_employees_joined_on"
            )
            collection_name = "employees"
        elif endpoint == "late_leaderboard":
            if month is None:
                raise HTTPException(422, "month is required for late_leaderboard")
            pipeline = _late_leaderboard_pipeline(month, limit, department)
            hint = "ix_attendance_date_late_emp"
            collection_name = "attendance_logs"
        else:
            if department is None or from_date is None or to_date is None:
                raise HTTPException(
                    422, "department, from, and to are required for department_trend"
                )
            if to_date < from_date:
                raise HTTPException(422, "to must not be before from")
            if (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1 > 92:
                raise HTTPException(422, "Date range must not exceed 92 days")
            pipeline = _trend_pipeline(department, from_date, to_date)
            hint = "ix_employees_department_joined"
            collection_name = "employees"
        explain = _explain_aggregation(database, collection_name, pipeline, hint)
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": json.loads(json_util.dumps(explain)),
    }
