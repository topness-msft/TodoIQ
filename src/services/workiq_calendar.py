"""Narrow typed facade for owned Work IQ calendar availability reads."""

from __future__ import annotations

from .workiq_policy import CalendarAction, build_calendar_operation
from .workiq_runtime import get_runtime


def find_meeting_times(body: dict, timeout: float) -> dict:
    operation = build_calendar_operation(CalendarAction.FIND_MEETING_TIMES, body)
    return get_runtime().execute_calendar(operation, timeout=timeout)


def get_schedule(body: dict, timeout: float) -> dict:
    operation = build_calendar_operation(CalendarAction.GET_SCHEDULE, body)
    return get_runtime().execute_calendar(operation, timeout=timeout)
