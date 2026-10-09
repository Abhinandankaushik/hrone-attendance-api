from datetime import date

import pytest

from app.main import (
    attendance_date,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    count_working_days,
    month_range,
)
from tests.conftest import ist

DAY_SHIFT = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT_SHIFT = {"shift_start": "22:00", "shift_end": "06:00"}


@pytest.mark.parametrize(
    "punch, expected",
    [
        ("2026-07-06 09:28:00", 0),
        ("2026-07-06 09:40:00", 0),
        ("2026-07-06 09:40:00.900", 0),
        ("2026-07-06 09:40:01", 10),
        ("2026-07-06 09:40:59", 10),
        ("2026-07-06 10:15:59", 45),
    ],
)
def test_late_minutes_follow_r2(punch, expected):
    assert compute_late_minutes(ist(punch), date(2026, 7, 6), DAY_SHIFT) == expected


def test_overnight_punch_after_midnight_belongs_to_previous_day_and_is_late():
    punch = ist("2026-07-07 00:30:00")
    assert attendance_date(punch, NIGHT_SHIFT) == date(2026, 7, 6)
    assert compute_late_minutes(punch, date(2026, 7, 6), NIGHT_SHIFT) == 150


@pytest.mark.parametrize(
    "punch, expected_day",
    [
        ("2026-07-06 21:55:00", date(2026, 7, 6)),
        ("2026-07-07 05:59:59", date(2026, 7, 6)),
        ("2026-07-07 06:00:00", date(2026, 7, 7)),
    ],
)
def test_overnight_attendance_date_boundary(punch, expected_day):
    assert attendance_date(ist(punch), NIGHT_SHIFT) == expected_day


def test_attendance_date_uses_ist_not_utc():
    assert attendance_date(ist("2026-07-07 01:00:00"), DAY_SHIFT) == date(2026, 7, 7)


@pytest.mark.parametrize(
    "punch_out, expected",
    [
        ("2026-07-06 18:35:00", 0),
        ("2026-07-06 18:59:59", 0),
        ("2026-07-06 19:00:00", 30),
        ("2026-07-06 19:10:59", 40),
        ("2026-07-06 17:00:00", 0),
    ],
)
def test_overtime_needs_thirty_minutes(punch_out, expected):
    assert compute_overtime_minutes(ist(punch_out), date(2026, 7, 6), DAY_SHIFT) == expected


def test_overnight_overtime_uses_next_day_shift_end():
    assert compute_overtime_minutes(ist("2026-07-07 06:40:00"), date(2026, 7, 6), NIGHT_SHIFT) == 40


@pytest.mark.parametrize(
    "punch_in, punch_out, expected",
    [
        ("2026-07-06 09:28:00", "2026-07-06 18:35:00", 9.12),
        ("2026-07-06 09:00:00", "2026-07-06 09:07:30", 0.13),
        ("2026-07-06 09:00:00", "2026-07-06 11:40:30", 2.68),
        ("2026-07-06 09:00:00.999", "2026-07-06 09:07:30.001", 0.13),
    ],
)
def test_work_hours_round_half_up(punch_in, punch_out, expected):
    assert compute_work_hours(ist(punch_in), ist(punch_out)) == expected


def test_month_range_and_working_days():
    assert month_range("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_range("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
    assert count_working_days(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert count_working_days(date(2026, 7, 15), date(2026, 7, 31)) == 13
    assert count_working_days(date(2026, 8, 10), date(2026, 7, 31)) == 0
