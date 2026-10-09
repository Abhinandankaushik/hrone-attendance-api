"""Seeds ~100,000 attendance records straight into MongoDB (no indexes), like the grader does.

Usage:  MONGO_URI=... MONGO_DB=attendance_scale python scripts/seed_large_dataset.py
It wipes the employees and attendance_logs collections of MONGO_DB first.
"""
import os
import random
from datetime import date, datetime, timedelta, timezone

from pymongo import MongoClient

IST = timezone(timedelta(hours=5, minutes=30))
DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "HR", "Ops", "Legal", "Design"]
EMPLOYEE_COUNT = 620
FIRST_DAY, LAST_DAY = date(2026, 1, 1), date(2026, 9, 30)

random.seed(7)
db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
db.employees.drop()
db.attendance_logs.drop()

staff = []
for number in range(1, EMPLOYEE_COUNT + 1):
    overnight = number % 10 == 0
    joined = FIRST_DAY + timedelta(days=random.choice([0, 0, 0, 30, 95, 180]))
    staff.append({
        "emp_code": f"EMP{number:04d}",
        "name": f"Employee {number}",
        "email": f"emp{number}@example.com",
        "department": DEPARTMENTS[number % len(DEPARTMENTS)],
        "shift_start": "22:00" if overnight else "09:30",
        "shift_end": "06:00" if overnight else "18:30",
        "joined_on": joined.isoformat(),
        "created_at": datetime.combine(joined, datetime.min.time(), tzinfo=IST),
    })
db.employees.insert_many(staff)

batch = []
for emp in staff:
    day = date.fromisoformat(emp["joined_on"])
    while day <= LAST_DAY:
        if day.weekday() < 5 or random.random() < 0.05:
            status = random.choices(["PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"], [70, 15, 5, 6, 4])[0]
            record = {"emp_code": emp["emp_code"], "date": day.isoformat(), "status": status, "punch_in": None,
                      "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                      "half_day": False, "history": []}
            if status in ("PRESENT", "WFH", "ON_DUTY"):
                start = datetime.combine(day, datetime.strptime(emp["shift_start"], "%H:%M").time(), tzinfo=IST)
                late = random.choice([0, 0, 0, 0, 12, 25, 40])
                punch_in = start + timedelta(minutes=late or -random.randint(0, 10))
                hours = random.choice([3.5, 8.5, 9.0, 9.25, 10.0])
                record |= {"punch_in": punch_in, "punch_out": punch_in + timedelta(hours=hours), "work_hours": hours,
                           "late_minutes": late, "overtime_minutes": random.choice([0, 0, 35, 60]), "half_day": hours < 4.5}
            batch.append(record)
        day += timedelta(days=1)
db.attendance_logs.insert_many(batch)
print(f"seeded {len(staff)} employees and {len(batch)} attendance records into {db.name}")
