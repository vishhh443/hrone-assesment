from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient

from app import main

IST = timezone(timedelta(hours=5, minutes=30))


class InMemoryMongoClient:
    def __init__(self, *_args, **_kwargs):
        self._client = mongomock.MongoClient(tz_aware=True)
        self.admin = SimpleNamespace(command=lambda *_args, **_kwargs: {"ok": 1})

    def __getitem__(self, name):
        return self._client[name]

    def close(self):
        self._client.close()


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("MONGO_URI", "mongodb+srv://test-user:test-password@example.invalid/")
    monkeypatch.setenv("MONGO_DB", "attendance_test_db")
    monkeypatch.setattr(main, "MongoClient", InMemoryMongoClient)
    with TestClient(main.app) as test_client:
        yield test_client


def to_epoch_millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def employee_payload(emp_code="EMP0001", **overrides):
    payload = {
        "emp_code": emp_code,
        "name": "Asha Rao",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    }
    payload.update(overrides)
    return payload


def make_employee(api, emp_code="EMP0001", **overrides):
    response = api.post("/employees", json=employee_payload(emp_code, **overrides))
    assert response.status_code == 201
    return response.json()


def test_health_checks_database_and_indexes(api):
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_uri_does_not_fall_back_to_local_mongodb(monkeypatch):
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.setattr(
        main,
        "MongoClient",
        lambda *_args, **_kwargs: pytest.fail("MongoClient must not use a localhost fallback"),
    )
    with TestClient(main.app) as test_client:
        response = test_client.get("/health")
    assert response.status_code == 503


def test_employee_create_duplicate_and_filtered_pagination(api):
    make_employee(api, "EMP0001", department="Engineering")
    make_employee(api, "EMP0002", department="Sales")
    make_employee(api, "EMP0003", department="Engineering")

    duplicate = api.post("/employees", json=employee_payload("EMP0001"))
    assert duplicate.status_code == 409

    page = api.get("/employees", params={"department": "Engineering", "page": 2, "page_size": 1})
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["items"][0]["emp_code"] == "EMP0003"
    assert "created_at" in page.json()["items"][0]
    assert "_id" not in page.json()["items"][0]

    assert api.get("/employees", params={"page_size": 101}).status_code == 422


def test_employee_validation_rejects_bad_shift_date_and_code(api):
    bad = employee_payload("not-an-employee", shift_start="09:30", shift_end="09:30")
    assert api.post("/employees", json=bad).status_code == 422
    bad = employee_payload("EMP0004", joined_on="2026-02-30")
    assert api.post("/employees", json=bad).status_code == 422


def test_punch_in_checks_employee_units_status_and_duplicate(api):
    instant = to_epoch_millis(datetime(2026, 7, 6, 9, 40, tzinfo=IST))
    unknown = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP9999", "punched_at": instant},
    )
    assert unknown.status_code == 404

    make_employee(api)
    created = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": instant, "late_minutes": 999},
    )
    assert created.status_code == 201
    assert created.json()["late_minutes"] == 0
    assert created.json()["punch_in"] == instant
    assert created.json()["history"] == []
    assert "id" not in created.json()

    assert api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": instant},
    ).status_code == 409


@pytest.mark.parametrize(
    ("local_time", "expected_late"),
    [((9, 40, 0), 0), ((9, 40, 1), 10), ((10, 15, 59), 45)],
)
def test_punch_in_late_grace_boundaries(api, local_time, expected_late):
    make_employee(api)
    punched_at = to_epoch_millis(datetime(2026, 7, 6, *local_time, tzinfo=IST))
    response = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punched_at},
    )
    assert response.status_code == 201
    assert response.json()["late_minutes"] == expected_late


def test_punch_in_truncates_milliseconds_and_handles_overnight_shift(api):
    make_employee(api, shift_start="22:00", shift_end="06:00")
    instant = to_epoch_millis(datetime(2026, 7, 7, 5, 30, 0, 987000, tzinfo=IST))
    response = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": instant, "status": "ON_DUTY"},
    )
    assert response.status_code == 201
    assert response.json()["date"] == "2026-07-06"
    assert response.json()["punch_in"] == instant - 987
    assert response.json()["status"] == "ON_DUTY"


@pytest.mark.parametrize("bad_timestamp", [1_783_312_500, 1.0, "1783312500000", None])
def test_punch_in_rejects_invalid_epoch_values(api, bad_timestamp):
    make_employee(api)
    response = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": bad_timestamp},
    )
    assert response.status_code == 422


def test_punch_out_computes_hours_overtime_and_rejects_repeat(api):
    make_employee(api)
    punch_in = to_epoch_millis(datetime(2026, 7, 6, 9, 30, tzinfo=IST))
    opened = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in},
    )
    assert opened.status_code == 201

    before = api.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": to_epoch_millis(datetime(2026, 7, 6, 9, 29, tzinfo=IST)),
        },
    )
    assert before.status_code == 422

    punch_out = to_epoch_millis(datetime(2026, 7, 6, 19, 0, tzinfo=IST))
    closed = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_out},
    )
    assert closed.status_code == 200
    assert closed.json()["work_hours"] == 9.5
    assert closed.json()["overtime_minutes"] == 30
    assert closed.json()["half_day"] is False

    repeat = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_out + 60_000},
    )
    assert repeat.status_code == 409


def test_punch_out_validates_more_than_24_hours(api):
    make_employee(api)
    punch_in = to_epoch_millis(datetime(2026, 7, 6, 9, 30, tzinfo=IST))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in})
    too_late = to_epoch_millis(datetime(2026, 7, 7, 9, 30, 1, tzinfo=IST))
    response = api.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": too_late},
    )
    assert response.status_code == 422


def test_punch_out_is_atomic_for_two_simultaneous_requests(api):
    make_employee(api)
    punch_in = to_epoch_millis(datetime(2026, 7, 6, 9, 30, tzinfo=IST))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in})
    body = {
        "emp_code": "EMP0001",
        "punched_at": to_epoch_millis(datetime(2026, 7, 6, 18, 30, tzinfo=IST)),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: api.post("/attendance/punch-out", json=body), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_half_up_work_hours_and_half_day_threshold():
    started = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
    tie_at_4_495_hours = started + timedelta(seconds=16_182)
    below_threshold = started + timedelta(seconds=16_164)
    assert main.compute_work_hours(started, started + timedelta(seconds=8_442)) == 2.35
    employee = {"shift_start": "09:30", "shift_end": "18:30"}
    assert main._computed_fields(
        "PRESENT", started, tie_at_4_495_hours, employee, "2026-07-06"
    )["half_day"] is False
    assert main._computed_fields(
        "PRESENT", started, below_threshold, employee, "2026-07-06"
    )["half_day"] is True


def test_attendance_filters_sorting_and_pagination(api):
    make_employee(api, "EMP0001")
    make_employee(api, "EMP0002")
    for code, day, status in (
        ("EMP0001", 6, "PRESENT"),
        ("EMP0002", 6, "WFH"),
        ("EMP0001", 7, "PRESENT"),
    ):
        timestamp = to_epoch_millis(datetime(2026, 7, day, 9, 30, tzinfo=IST))
        assert api.post(
            "/attendance/punch-in",
            json={"emp_code": code, "punched_at": timestamp, "status": status},
        ).status_code == 201

    response = api.get(
        "/attendance",
        params={"date_from": "2026-07-06", "date_to": "2026-07-07", "page": 1, "page_size": 2},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert [item["date"] for item in response.json()["items"]] == [
        "2026-07-07",
        "2026-07-06",
    ]
    assert response.json()["items"][1]["emp_code"] == "EMP0001"
    assert api.get(
        "/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-01"}
    ).status_code == 422
    assert api.get("/attendance", params={"date_from": "not-a-date"}).status_code == 422


def test_regularization_appends_only_real_changes_and_serializes_history(api):
    make_employee(api)
    punch_in = to_epoch_millis(datetime(2026, 7, 6, 10, 5, tzinfo=IST))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in})
    corrected = to_epoch_millis(datetime(2026, 7, 6, 9, 28, tzinfo=IST))
    response = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "punch_in": corrected,
            "reason": "biometric correction",
            "regularized_by": "hr.admin",
            "late_minutes": 1234,
        },
    )
    assert response.status_code == 200
    record = response.json()
    assert record["punch_in"] == corrected
    assert record["late_minutes"] == 0
    assert len(record["history"]) == 1
    assert record["history"][0]["changes"]["punch_in"] == {
        "from": punch_in,
        "to": corrected,
    }
    no_change = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "punch_in": corrected,
            "reason": "same timestamp",
            "regularized_by": "hr.admin",
        },
    )
    assert no_change.status_code == 422


def test_regularization_status_rules_and_path_date_validation(api):
    make_employee(api)
    assert api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"reason": "not found", "regularized_by": "hr.admin", "status": "LEAVE"},
    ).status_code == 404
    assert api.patch(
        "/attendance/EMP0001/not-a-date",
        json={"reason": "invalid date", "regularized_by": "hr.admin"},
    ).status_code == 422

    punch_in = to_epoch_millis(datetime(2026, 7, 6, 9, 30, tzinfo=IST))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in})
    invalid = api.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "LEAVE",
            "punch_in": punch_in,
            "reason": "incorrect status",
            "regularized_by": "hr.admin",
        },
    )
    assert invalid.status_code == 422


def test_analytics_pipeline_builders_use_database_side_operators():
    monthly = main._employee_monthly_pipeline("EMP0001", "2026-07")
    summary = main._department_summary_pipeline("2026-07", None)
    leaderboard = main._late_leaderboard_pipeline("2026-07", 2, "Engineering")
    trend = main._trend_pipeline("Engineering", "2026-07-01", "2026-07-31")

    assert any("$lookup" in stage for stage in monthly)
    assert any("$lookup" in stage for stage in summary)
    assert any("$setWindowFields" in stage for stage in leaderboard)
    assert any("$setWindowFields" in stage for stage in trend)
    assert any("$range" in str(stage) for stage in trend)
    assert leaderboard[-3] == {"$match": {"rank": {"$lte": 2}}}


def test_monthly_and_department_analytics_response_shapes(api, monkeypatch):
    make_employee(api)
    employees = main.db.employees
    monkeypatch.setattr(
        type(employees),
        "aggregate",
        lambda _self, _pipeline, **_kwargs: iter(
            [{"emp_code": "EMP0001", "month": "2026-07", "working_days": 23,
              "present_days": 10.5, "leave_days": 1, "late_count": 2,
              "total_late_minutes": 35, "total_overtime_minutes": 30}]
        ),
    )
    monthly = api.get(
        "/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}
    )
    assert monthly.status_code == 200
    assert monthly.json()["attendance_pct"] == 45.65

    monkeypatch.setattr(
        type(employees),
        "aggregate",
        lambda _self, _pipeline, **_kwargs: iter(
            [{"department": "Engineering", "headcount": 1, "present_days": 10.5,
              "avg_work_hours": 9.125, "late_count": 2,
              "total_late_minutes": 35, "leave_count": 1, "on_duty_count": 0}]
        ),
    )
    summary = api.get(
        "/analytics/departments/summary", params={"month": "2026-07"}
    )
    assert summary.status_code == 200
    assert summary.json()["items"][0]["avg_work_hours"] == 9.13


def test_leaderboard_trend_and_explain_response_shapes(api, monkeypatch):
    make_employee(api)
    attendance = main.db.attendance_logs
    monkeypatch.setattr(
        type(attendance),
        "aggregate",
        lambda _self, _pipeline, **_kwargs: iter(
            [{"rank": 1, "emp_code": "EMP0001", "name": "Asha Rao",
              "department": "Engineering", "total_late_minutes": 35, "late_count": 1}]
        ),
    )
    leaderboard = api.get(
        "/analytics/leaderboard/late", params={"month": "2026-07"}
    )
    assert leaderboard.status_code == 200
    assert leaderboard.json()["items"][0]["rank"] == 1

    employees = main.db.employees
    monkeypatch.setattr(
        type(employees),
        "aggregate",
        lambda _self, _pipeline, **_kwargs: iter(
            [{"date": "2026-07-06", "is_working_day": True, "headcount": 1,
              "present_count": 0.5, "late_count": 0, "attendance_rate": 0.5,
              "moving_avg_7d": 0.5}]
        ),
    )
    trend = api.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2026-07-06", "to": "2026-07-06"},
    )
    assert trend.status_code == 200
    assert trend.json()["items"][0]["attendance_rate"] == 0.5

    monkeypatch.setattr(
        type(main.db),
        "command",
        lambda _self, *_args, **_kwargs: {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}},
    )
    explain = api.get(
        "/admin/explain/department_trend",
        params={
            "department": "Engineering",
            "from": "2026-07-06",
            "to": "2026-07-06",
        },
    )
    assert explain.status_code == 200
    assert explain.json()["explain"]["queryPlanner"]["winningPlan"]["stage"] == "IXSCAN"


def test_legacy_attendance_fields_get_documented_defaults():
    legacy = {
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": None,
        "punch_out": None,
    }
    serialized = main._serialize_attendance(legacy)
    assert serialized["half_day"] is False
    assert serialized["history"] == []
    assert serialized["late_minutes"] == 0
    assert serialized["overtime_minutes"] == 0


def test_explain_requires_endpoint_specific_parameters(api):
    response = api.get("/admin/explain/employee_monthly", params={"emp_code": "EMP0001"})
    assert response.status_code == 422
    response = api.get(
        "/admin/explain/department_trend",
        params={"department": "Engineering", "from": "2026-07-01"},
    )
    assert response.status_code == 422


def test_duplicate_concurrent_punch_ins_are_database_enforced(api):
    make_employee(api)
    body = {
        "emp_code": "EMP0001",
        "punched_at": to_epoch_millis(datetime(2026, 7, 6, 9, 30, tzinfo=IST)),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: api.post("/attendance/punch-in", json=body), range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
