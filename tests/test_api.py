import pytest

from tests.conftest import employee, ist, ist_ms

RECORD_FIELDS = {
    "emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
    "late_minutes", "overtime_minutes", "half_day", "history",
}


def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_create_employee_returns_contract_shape(api):
    response = api.post("/employees", json=employee("EMP0001"))
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert isinstance(body["created_at"], int) and body["created_at"] > 100_000_000_000


def test_duplicate_employee_is_409(api):
    api.post("/employees", json=employee("EMP0001"))
    assert api.post("/employees", json=employee("EMP0001", name="Other")).status_code == 409


@pytest.mark.parametrize(
    "overrides",
    [
        {"emp_code": "E0001"},
        {"emp_code": "EMP12"},
        {"email": "not-an-email"},
        {"shift_start": "24:00"},
        {"shift_start": "9:30"},
        {"shift_start": "10:00", "shift_end": "10:00"},
        {"joined_on": "2026-02-30"},
        {"joined_on": "05-01-2026"},
        {"name": ""},
        {"department": "x" * 51},
    ],
)
def test_invalid_employee_is_422(api, overrides):
    assert api.post("/employees", json=employee("EMP0001") | overrides).status_code == 422


def test_list_employees_filters_sorts_and_paginates(api):
    for code, department in [("EMP0003", "Sales"), ("EMP0001", "Engineering"), ("EMP0002", "Sales"), ("EMP0004", "sales")]:
        api.post("/employees", json=employee(code, department=department))

    page = api.get("/employees", params={"department": "Sales"}).json()
    assert [e["emp_code"] for e in page["items"]] == ["EMP0002", "EMP0003"]
    assert page["total"] == 2

    first = api.get("/employees", params={"page": 1, "page_size": 3}).json()
    second = api.get("/employees", params={"page": 2, "page_size": 3}).json()
    assert [e["emp_code"] for e in first["items"]] == ["EMP0001", "EMP0002", "EMP0003"]
    assert [e["emp_code"] for e in second["items"]] == ["EMP0004"]
    assert first["total"] == second["total"] == 4
    assert "_id" not in first["items"][0]


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_bad_pagination_is_422(api, params):
    assert api.get("/employees", params=params).status_code == 422
    assert api.get("/attendance", params=params).status_code == 422


def test_punch_in_unknown_employee_is_404(api):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ist_ms("2026-07-06 09:30:00")}).status_code == 404


def test_punch_in_creates_record(api):
    api.post("/employees", json=employee("EMP0001"))
    response = api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 09:40:01") + 900, "status": "WFH"},
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == RECORD_FIELDS
    assert body == {
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "WFH",
        "punch_in": ist_ms("2026-07-06 09:40:01"),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 10,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


def test_punch_in_without_time_uses_now(api):
    api.post("/employees", json=employee("EMP0001"))
    response = api.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
    assert response.status_code == 201
    assert response.json()["punch_in"] % 1000 == 0


def test_punch_in_twice_same_day_is_409(api):
    api.post("/employees", json=employee("EMP0001"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 09:30:00")})
    second = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 13:00:00")})
    assert second.status_code == 409


def test_overnight_punch_in_is_dated_to_shift_start(api):
    api.post("/employees", json=employee("EMP0005", shift_start="22:00", shift_end="06:00"))
    response = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ist_ms("2026-07-07 00:30:00")})
    assert response.json()["date"] == "2026-07-06"
    assert response.json()["late_minutes"] == 150


@pytest.mark.parametrize(
    "payload",
    [
        {"punched_at": 1783311001},
        {"punched_at": 1783311001000.0},
        {"punched_at": "1783311001000"},
        {"punched_at": True},
        {"punched_at": None},
        {"punched_at": 4102444800001},
        {"status": "ABSENT"},
        {"status": "present"},
    ],
)
def test_invalid_punch_in_is_422(api, payload):
    api.post("/employees", json=employee("EMP0001"))
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001"} | payload).status_code == 422


def test_punch_out_computes_derived_fields(api):
    api.post("/employees", json=employee("EMP0003"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": ist_ms("2026-07-06 10:05:00")})
    response = api.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": ist_ms("2026-07-06 19:10:00")})
    assert response.status_code == 200
    body = response.json()
    assert (body["work_hours"], body["late_minutes"], body["overtime_minutes"], body["half_day"]) == (9.08, 35, 40, False)
    assert body["history"] == []


def test_short_day_is_half_day(api):
    api.post("/employees", json=employee("EMP0004"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": ist_ms("2026-07-07 09:30:00")})
    body = api.post("/attendance/punch-out", json={"emp_code": "EMP0004", "punched_at": ist_ms("2026-07-07 13:00:00")}).json()
    assert (body["work_hours"], body["half_day"]) == (3.5, True)


def test_rounded_work_hours_decide_half_day(api):
    api.post("/employees", json=employee("EMP0004"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": ist_ms("2026-07-07 09:30:00")})
    body = api.post("/attendance/punch-out", json={"emp_code": "EMP0004", "punched_at": ist_ms("2026-07-07 13:59:42")}).json()
    assert (body["work_hours"], body["half_day"]) == (4.5, False)


def test_overnight_punch_out_closes_previous_day(api):
    api.post("/employees", json=employee("EMP0005", shift_start="22:00", shift_end="06:00"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ist_ms("2026-07-06 21:55:00")})
    body = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ist_ms("2026-07-07 06:40:00")}).json()
    assert (body["date"], body["work_hours"], body["overtime_minutes"]) == ("2026-07-06", 8.75, 40)


def test_punch_out_errors(api):
    api.post("/employees", json=employee("EMP0001"))
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 18:00:00")}).status_code == 404

    punched_in = ist_ms("2026-07-06 09:30:00")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punched_in})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punched_in}).status_code == 422
    too_late = punched_in + 24 * 3600 * 1000 + 1000
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": too_late}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 12345}).status_code == 422

    ok = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 18:30:00")})
    assert ok.status_code == 200
    again = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 18:45:00")})
    assert again.status_code == 409


def seed_logs(db, docs):
    db.attendance_logs.insert_many([dict(doc) for doc in docs])


def test_list_attendance_sorts_filters_and_reads_legacy_records(api, db):
    for code in ("EMP0001", "EMP0002"):
        api.post("/employees", json=employee(code))
    seed_logs(db, [
        {"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT", "punch_in": ist("2026-07-14 09:32:00"),
         "punch_out": ist("2026-07-14 18:31:00"), "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-14", "status": "LEAVE", "punch_in": None, "punch_out": None,
         "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": "EMP0001", "date": "2026-07-13", "status": "ABSENT", "punch_in": None, "punch_out": None,
         "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
    ])
    page = api.get("/attendance").json()
    assert [(r["date"], r["emp_code"]) for r in page["items"]] == [
        ("2026-07-14", "EMP0001"), ("2026-07-14", "EMP0002"), ("2026-07-13", "EMP0001"),
    ]
    legacy = page["items"][1]
    assert set(legacy) == RECORD_FIELDS
    assert (legacy["half_day"], legacy["history"], legacy["punch_in"]) == (False, [], ist_ms("2026-07-14 09:32:00"))

    filtered = api.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-14", "date_to": "2026-07-14"}).json()
    assert filtered["total"] == 1 and filtered["items"][0]["status"] == "LEAVE"
    assert api.get("/attendance", params={"status": "ABSENT"}).json()["total"] == 1
    assert api.get("/attendance", params={"page": 2, "page_size": 2}).json()["total"] == 3
    assert api.get("/attendance", params={"date_from": "2026-07-15", "date_to": "2026-07-14"}).status_code == 422
    assert api.get("/attendance", params={"status": "LATE"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-13-01"}).status_code == 422


def punched_day(api, code="EMP0001"):
    api.post("/employees", json=employee(code))
    api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ist_ms("2026-07-07 10:05:00")})
    api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ist_ms("2026-07-07 18:30:00")})


def test_regularize_recomputes_and_appends_history(api):
    punched_day(api)
    response = api.patch(
        "/attendance/EMP0001/2026-07-07",
        json={"punch_in": ist_ms("2026-07-07 09:28:00"), "reason": "biometric glitch", "regularized_by": "hr.admin",
              "late_minutes": 999, "work_hours": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["late_minutes"], body["work_hours"], body["half_day"]) == (0, 9.03, False)
    [entry] = body["history"]
    assert (entry["by"], entry["reason"]) == ("hr.admin", "biometric glitch")
    assert isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ist_ms("2026-07-07 10:05:00"), "to": ist_ms("2026-07-07 09:28:00")},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }

    second = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "WFH", "reason": "worked from home", "regularized_by": "hr"})
    assert second.status_code == 200
    history = second.json()["history"]
    assert len(history) == 2 and history[0] == entry
    assert history[1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_regularize_to_leave_clears_punches(api):
    punched_day(api)
    body = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr"}).json()
    assert (body["punch_in"], body["punch_out"], body["work_hours"], body["late_minutes"]) == (None, None, None, 0)
    assert set(body["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}
    assert body["history"][0]["changes"]["punch_out"]["to"] is None


def test_regularize_absent_to_present_needs_punch_in(api, db):
    api.post("/employees", json=employee("EMP0001"))
    seed_logs(db, [{"emp_code": "EMP0001", "date": "2026-07-08", "status": "ABSENT", "punch_in": None, "punch_out": None,
                    "work_hours": None, "late_minutes": 0, "overtime_minutes": 0}])
    url = "/attendance/EMP0001/2026-07-08"
    assert api.patch(url, json={"status": "PRESENT", "reason": "was in office", "regularized_by": "hr"}).status_code == 422
    body = api.patch(url, json={"status": "PRESENT", "punch_in": ist_ms("2026-07-08 09:45:00"),
                                "punch_out": ist_ms("2026-07-08 18:00:00"), "reason": "was in office", "regularized_by": "hr"}).json()
    assert (body["status"], body["late_minutes"], body["work_hours"]) == ("PRESENT", 15, 8.25)
    assert len(body["history"]) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ABSENT", "punch_in": ist_ms("2026-07-07 09:30:00")},
        {"punch_in": ist_ms("2026-07-08 09:30:00")},
        {"punch_out": ist_ms("2026-07-07 09:00:00")},
        {"punch_out": ist_ms("2026-07-08 11:00:00")},
        {"punch_in": ist_ms("2026-07-07 10:05:00")},
        {},
        {"status": "BUSY"},
        {"punch_in": None},
    ],
)
def test_invalid_regularization_is_422(api, payload):
    punched_day(api)
    body = {"reason": "correction", "regularized_by": "hr"} | payload
    assert api.patch("/attendance/EMP0001/2026-07-07", json=body).status_code == 422


def test_regularization_validation_of_reason_and_path(api):
    punched_day(api)
    assert api.patch("/attendance/EMP0001/2026-07-07", json={"status": "WFH", "reason": "abc", "regularized_by": "hr"}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-07", json={"status": "WFH", "reason": "valid reason"}).status_code == 422
    assert api.patch("/attendance/EMP0001/07-07-2026", json={"status": "WFH", "reason": "valid reason", "regularized_by": "hr"}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-09", json={"status": "WFH", "reason": "valid reason", "regularized_by": "hr"}).status_code == 404
    assert api.patch("/attendance/EMP9999/2026-07-07", json={"status": "WFH", "reason": "valid reason", "regularized_by": "hr"}).status_code == 404


def test_huge_page_is_empty_or_422_never_500(api):
    api.post("/employees", json=employee("EMP0001"))
    for path in ("/employees", "/attendance"):
        big = api.get(path, params={"page": 10**15, "page_size": 100})
        assert big.status_code == 200 and big.json()["items"] == [] and big.json()["total"] >= 0
        assert api.get(path, params={"page": 10**18}).status_code == 422


def test_punch_out_before_punch_in_is_422(api):
    api.post("/employees", json=employee("EMP0001"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 09:30:00")})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 18:30:00")})
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-07 10:00:00")})
    early = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-07 09:00:00")})
    assert early.status_code == 422


def test_regularization_that_only_restates_values_is_422(api, db):
    api.post("/employees", json=employee("EMP0001"))
    seed_logs(db, [{"emp_code": "EMP0001", "date": "2026-07-08", "status": "PRESENT", "punch_in": ist("2026-07-08 10:30:00"),
                    "punch_out": None, "work_hours": None, "late_minutes": 5, "overtime_minutes": 0}])
    response = api.patch("/attendance/EMP0001/2026-07-08", json={"status": "PRESENT", "reason": "no real change", "regularized_by": "hr"})
    assert response.status_code == 422
