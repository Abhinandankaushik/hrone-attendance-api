"""Analytics checked against a hand-computed dataset for July 2026 (1 July is a Wednesday, 23 working days).

Engineering: EMP1001 (full month), EMP1002 (joins 15 July), EMP1003 (joins in August, not counted for July).
Sales:       EMP1004 (no logs at all), EMP1005 (overnight shift).
Ops:         EMP1006 joins in September, so Ops has no July headcount and is omitted.
EMP9999 has logs but no employee document; it must be ignored.
"""
import pytest

from tests.conftest import ist

EMPLOYEES = [
    ("EMP1001", "Engineering", "2026-01-01", "09:30", "18:30"),
    ("EMP1002", "Engineering", "2026-07-15", "09:30", "18:30"),
    ("EMP1003", "Engineering", "2026-08-10", "09:30", "18:30"),
    ("EMP1004", "Sales", "2026-01-01", "09:30", "18:30"),
    ("EMP1005", "Sales", "2026-03-01", "22:00", "06:00"),
    ("EMP1006", "Ops", "2026-09-01", "09:30", "18:30"),
]


def log(code, day, status, work_hours=None, late=0, overtime=0, half_day=False, legacy=False):
    present = status in ("PRESENT", "WFH", "ON_DUTY")
    doc = {
        "emp_code": code,
        "date": day,
        "status": status,
        "punch_in": ist(f"{day} 09:30:00") if present else None,
        "punch_out": ist(f"{day} 18:30:00") if present and work_hours is not None else None,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
    }
    if not legacy:
        doc |= {"half_day": half_day, "history": []}
    return doc


LOGS = [
    log("EMP1001", "2026-07-01", "PRESENT", 9.0, late=15),
    log("EMP1001", "2026-07-02", "WFH", 3.5, half_day=True),
    log("EMP1001", "2026-07-03", "ON_DUTY", 8.0),
    log("EMP1001", "2026-07-04", "PRESENT", 5.0, late=20, overtime=40),
    log("EMP1001", "2026-07-06", "LEAVE"),
    log("EMP1001", "2026-07-07", "ABSENT"),
    log("EMP1001", "2026-07-08", "PRESENT"),
    log("EMP1001", "2026-07-09", "PRESENT", 8.98, legacy=True),
    log("EMP1001", "2026-06-30", "PRESENT", 9.0, late=99),
    log("EMP1002", "2026-07-15", "PRESENT", 9.12, late=25),
    log("EMP1002", "2026-07-16", "PRESENT", 9.13, late=25),
    log("EMP1005", "2026-07-06", "PRESENT", 8.74, overtime=40),
    log("EMP1005", "2026-07-10", "PRESENT", 8.75, late=50),
    log("EMP9999", "2026-07-01", "PRESENT", 9.0, late=500),
]


@pytest.fixture
def dataset(api, db):
    db.employees.insert_many([
        {"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@example.com", "department": department,
         "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": ist(f"{joined} 09:00:00")}
        for code, department, joined, start, end in EMPLOYEES
    ])
    db.attendance_logs.insert_many([dict(doc) for doc in LOGS])
    return api


def test_employee_monthly(dataset):
    assert dataset.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"}).json() == {
        "emp_code": "EMP1001",
        "month": "2026-07",
        "working_days": 23,
        "present_days": 4.5,
        "leave_days": 1,
        "late_count": 2,
        "total_late_minutes": 35,
        "total_overtime_minutes": 40,
        "attendance_pct": 19.57,
    }


def test_employee_monthly_mid_month_joiner(dataset):
    body = dataset.get("/analytics/employees/EMP1002/monthly", params={"month": "2026-07"}).json()
    assert (body["working_days"], body["present_days"], body["attendance_pct"]) == (13, 2, 15.38)


def test_employee_monthly_without_logs_or_before_joining(dataset):
    no_logs = dataset.get("/analytics/employees/EMP1004/monthly", params={"month": "2026-07"}).json()
    assert (no_logs["working_days"], no_logs["present_days"], no_logs["attendance_pct"]) == (23, 0, 0.0)
    not_joined = dataset.get("/analytics/employees/EMP1003/monthly", params={"month": "2026-07"}).json()
    assert (not_joined["working_days"], not_joined["attendance_pct"]) == (0, None)


def test_employee_monthly_errors(dataset):
    assert dataset.get("/analytics/employees/EMP0404/monthly", params={"month": "2026-07"}).status_code == 404
    assert dataset.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-13"}).status_code == 422
    assert dataset.get("/analytics/employees/EMP1001/monthly").status_code == 422


def test_department_summary(dataset):
    assert dataset.get("/analytics/departments/summary", params={"month": "2026-07"}).json() == {
        "month": "2026-07",
        "items": [
            {"department": "Engineering", "headcount": 2, "present_days": 6.5, "avg_work_hours": 7.53,
             "late_count": 4, "total_late_minutes": 85, "leave_count": 1, "on_duty_count": 1},
            {"department": "Sales", "headcount": 2, "present_days": 2.0, "avg_work_hours": 8.75,
             "late_count": 1, "total_late_minutes": 50, "leave_count": 0, "on_duty_count": 0},
        ],
    }


def test_department_summary_filter_and_empty_department(dataset):
    sales = dataset.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert [item["department"] for item in sales["items"]] == ["Sales"]
    ops = dataset.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json()
    assert ops["items"] == []
    september = dataset.get("/analytics/departments/summary", params={"month": "2026-09", "department": "Ops"}).json()
    assert september["items"] == [
        {"department": "Ops", "headcount": 1, "present_days": 0.0, "avg_work_hours": None,
         "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0},
    ]


def ranking(response):
    return [(row["rank"], row["emp_code"], row["total_late_minutes"], row["late_count"]) for row in response.json()["items"]]


def test_late_leaderboard_ranks_ties_and_skips(dataset):
    response = dataset.get("/analytics/leaderboard/late", params={"month": "2026-07"})
    assert ranking(response) == [(1, "EMP1002", 50, 2), (1, "EMP1005", 50, 1), (3, "EMP1001", 35, 2)]
    first = response.json()["items"][0]
    assert (first["name"], first["department"]) == ("Name EMP1002", "Engineering")


@pytest.mark.parametrize("limit, codes", [(1, ["EMP1002", "EMP1005"]), (2, ["EMP1002", "EMP1005"]), (3, ["EMP1002", "EMP1005", "EMP1001"])])
def test_late_leaderboard_limit_applies_after_ranking(dataset, limit, codes):
    response = dataset.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": limit})
    assert [row[1] for row in ranking(response)] == codes


def test_late_leaderboard_ranks_inside_department(dataset):
    response = dataset.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Engineering"})
    assert ranking(response) == [(1, "EMP1002", 50, 2), (2, "EMP1001", 35, 2)]


@pytest.mark.parametrize("limit", [0, 51, "x"])
def test_late_leaderboard_bad_limit(dataset, limit):
    assert dataset.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": limit}).status_code == 422


def test_department_trend_fills_gaps_and_averages(dataset):
    response = dataset.get("/analytics/departments/Engineering/trend", params={"from": "2026-06-29", "to": "2026-07-12"})
    assert response.status_code == 200
    rows = response.json()["items"]
    expected = [
        ("2026-06-29", True, 1, 0, 0, 0.0, 0.0),
        ("2026-06-30", True, 1, 1, 1, 1.0, 0.5),
        ("2026-07-01", True, 1, 1, 1, 1.0, 0.6667),
        ("2026-07-02", True, 1, 0.5, 0, 0.5, 0.625),
        ("2026-07-03", True, 1, 1, 0, 1.0, 0.7),
        ("2026-07-04", False, 1, 1, 1, None, 0.7),
        ("2026-07-05", False, 1, 0, 0, None, 0.7),
        ("2026-07-06", True, 1, 0, 0, 0.0, 0.7),
        ("2026-07-07", True, 1, 0, 0, 0.0, 0.5),
        ("2026-07-08", True, 1, 1, 0, 1.0, 0.5),
        ("2026-07-09", True, 1, 1, 0, 1.0, 0.6),
        ("2026-07-10", True, 1, 0, 0, 0.0, 0.4),
        ("2026-07-11", False, 1, 0, 0, None, 0.4),
        ("2026-07-12", False, 1, 0, 0, None, 0.4),
    ]
    actual = [
        (r["date"], r["is_working_day"], r["headcount"], r["present_count"], r["late_count"], r["attendance_rate"], r["moving_avg_7d"])
        for r in rows
    ]
    assert actual == expected
    assert set(rows[0]) == {"date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"}


def test_department_trend_headcount_grows_with_joiners(dataset):
    rows = dataset.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-14", "to": "2026-07-16"}).json()["items"]
    assert [(r["date"], r["headcount"], r["attendance_rate"]) for r in rows] == [
        ("2026-07-14", 1, 0.0),
        ("2026-07-15", 2, 0.5),
        ("2026-07-16", 2, 0.5),
    ]
    assert [r["moving_avg_7d"] for r in rows] == [0.0, 0.25, 0.3333]


def test_department_trend_before_anyone_joined(dataset):
    rows = dataset.get("/analytics/departments/Ops/trend", params={"from": "2026-07-01", "to": "2026-07-03"}).json()["items"]
    assert [(r["headcount"], r["attendance_rate"], r["moving_avg_7d"]) for r in rows] == [(0, None, None)] * 3


def test_department_trend_errors(dataset):
    url = "/analytics/departments/Engineering/trend"
    assert dataset.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert dataset.get(url, params={"from": "2026-07-02", "to": "2026-07-01"}).status_code == 422
    assert len(dataset.get(url, params={"from": "2026-07-01", "to": "2026-09-30"}).json()["items"]) == 92
    assert dataset.get(url, params={"from": "2026-06-30", "to": "2026-09-30"}).status_code == 422
    assert dataset.get(url, params={"from": "2026-07-01"}).status_code == 422
    assert dataset.get(url, params={"from": "2026-07-01", "to": "2026-07-32"}).status_code == 422


@pytest.mark.parametrize(
    "endpoint, params",
    [
        ("attendance_list", {}),
        ("attendance_list", {"emp_code": "EMP1001", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("attendance_list", {"status": "LEAVE", "page": 1, "page_size": 5}),
        ("employee_monthly", {"emp_code": "EMP1001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("department_summary", {"month": "2026-07", "department": "Sales"}),
        ("late_leaderboard", {"month": "2026-07", "limit": 2}),
        ("late_leaderboard", {"month": "2026-07", "department": "Engineering"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
    ],
)
def test_explain_uses_indexes(dataset, endpoint, params):
    response = dataset.get(f"/admin/explain/{endpoint}", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["endpoint"] == endpoint and isinstance(body["explain"], dict)
    assert "IXSCAN" in response.text and "COLLSCAN" not in response.text


@pytest.mark.parametrize(
    "endpoint, params",
    [
        ("employee_monthly", {"emp_code": "EMP1001"}),
        ("employee_monthly", {"month": "2026-07"}),
        ("department_summary", {}),
        ("late_leaderboard", {}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01"}),
        ("department_trend", {"from": "2026-07-01", "to": "2026-07-31"}),
        ("unknown_endpoint", {}),
    ],
)
def test_explain_validates_parameters(dataset, endpoint, params):
    assert dataset.get(f"/admin/explain/{endpoint}", params=params).status_code == 422


@pytest.mark.parametrize(
    "path",
    ["/analytics/employees/EMP1001/monthly", "/analytics/departments/summary", "/analytics/leaderboard/late", "/admin/explain/late_leaderboard"],
)
def test_extreme_months(dataset, path):
    params = {"emp_code": "EMP1001"}
    assert dataset.get(path, params=params | {"month": "0000-01"}).status_code == 422
    assert dataset.get(path, params=params | {"month": "9999-12"}).status_code == 200


def test_trend_at_the_end_of_the_calendar(dataset):
    rows = dataset.get("/analytics/departments/Engineering/trend", params={"from": "9999-12-30", "to": "9999-12-31"}).json()["items"]
    assert [row["date"] for row in rows] == ["9999-12-30", "9999-12-31"]
