import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from tests.conftest import employee, ist_ms

PARALLEL = 12


def fire_together(server, method, path, payloads):
    barrier = threading.Barrier(len(payloads))

    def send(payload):
        with httpx.Client(base_url=server, timeout=30) as client:
            barrier.wait()
            return client.request(method, path, json=payload)

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(send, payloads))


def test_parallel_employee_creates_yield_one_201(api, server):
    responses = fire_together(server, "POST", "/employees", [employee("EMP0100")] * PARALLEL)
    assert Counter(r.status_code for r in responses) == {201: 1, 409: PARALLEL - 1}


def test_parallel_punch_ins_yield_one_201(api, server, db):
    api.post("/employees", json=employee("EMP0001"))
    payload = {"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 09:30:00")}
    responses = fire_together(server, "POST", "/attendance/punch-in", [payload] * PARALLEL)
    assert Counter(r.status_code for r in responses) == {201: 1, 409: PARALLEL - 1}
    assert db.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


def test_parallel_punch_outs_yield_one_200(api, server):
    api.post("/employees", json=employee("EMP0001"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 09:30:00")})
    payload = {"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 18:30:00")}
    responses = fire_together(server, "POST", "/attendance/punch-out", [payload] * PARALLEL)
    assert Counter(r.status_code for r in responses) == {200: 1, 409: PARALLEL - 1}


def test_parallel_regularizations_never_lose_history(api, server, db):
    api.post("/employees", json=employee("EMP0001"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 09:30:00")})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms("2026-07-06 18:30:00")})
    payloads = [
        {"punch_out": ist_ms(f"2026-07-06 19:{minute:02d}:00"), "reason": f"correction {minute}", "regularized_by": f"hr{minute}"}
        for minute in range(PARALLEL)
    ]
    responses = fire_together(server, "PATCH", "/attendance/EMP0001/2026-07-06", payloads)
    codes = Counter(r.status_code for r in responses)
    assert set(codes) <= {200, 409} and codes[200] >= 1

    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    winners = {r.json()["history"][-1]["reason"] for r in responses if r.status_code == 200}
    assert len(stored["history"]) == codes[200]
    assert {entry["reason"] for entry in stored["history"]} == winners
