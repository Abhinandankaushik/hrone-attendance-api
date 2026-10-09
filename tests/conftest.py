import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pymongo import MongoClient

MONGO_URI = os.environ.get("TEST_MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("TEST_MONGO_DB", "attendance_api_tests")
PORT = int(os.environ.get("TEST_PORT", "8765"))
ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))

os.environ.setdefault("MONGO_URI", MONGO_URI)
os.environ.setdefault("MONGO_DB", MONGO_DB)


def ist(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=IST).astimezone(timezone.utc)


def ist_ms(text: str) -> int:
    return int(ist(text).timestamp() * 1000)


def employee(code: str, **overrides) -> dict:
    return {
        "emp_code": code,
        "name": f"Name {code}",
        "email": f"{code.lower()}@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    } | overrides


@pytest.fixture(scope="session")
def db():
    client = MongoClient(MONGO_URI, tz_aware=True)
    client.drop_database(MONGO_DB)
    yield client[MONGO_DB]
    client.drop_database(MONGO_DB)
    client.close()


@pytest.fixture(scope="session")
def server(db):
    env = os.environ | {"MONGO_URI": MONGO_URI, "MONGO_DB": MONGO_DB}
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{PORT}"
    deadline = time.monotonic() + 20
    while True:
        try:
            if httpx.get(f"{base_url}/health").status_code == 200:
                break
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            process.kill()
            raise RuntimeError("the app did not become healthy within 20 seconds")
        time.sleep(0.2)
    yield base_url
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture
def api(server, db):
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    with httpx.Client(base_url=server, timeout=30) as client:
        yield client
