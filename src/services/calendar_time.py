"""Timezone-aware validation for calendar event instants."""

from datetime import datetime, timezone

from dateutil import tz


def _aware_local(value, timezone_name):
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    zone = tz.gettz(str(timezone_name or "").strip())
    if zone is None:
        return None
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        named = parsed.replace(tzinfo=None).replace(tzinfo=zone)
        if (
            not tz.datetime_exists(named)
            or tz.datetime_ambiguous(named)
            or named.utcoffset() != parsed.utcoffset()
        ):
            return None
        return parsed
    localized = parsed.replace(tzinfo=zone)
    if not tz.datetime_exists(localized) or tz.datetime_ambiguous(localized):
        return None
    return localized


def named_timezone_matches(value, timezone_name) -> bool:
    """Does an offset-aware value agree with the declared timezone at that wall time?"""
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False
    zone = tz.gettz(str(timezone_name or "").strip())
    if zone is None:
        return False
    named = parsed.replace(tzinfo=None).replace(tzinfo=zone)
    return (
        tz.datetime_exists(named)
        and not tz.datetime_ambiguous(named)
        and named.utcoffset() == parsed.utcoffset()
    )


def calendar_event_is_future(event, now=None) -> bool:
    """Compare a CreateEvent start to an aware current instant."""
    if not isinstance(event, dict):
        return False
    start = _aware_local(event.get("start"), event.get("time_zone"))
    if start is None:
        return False
    if now is None:
        current = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        current = now
    else:
        try:
            current = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
    if current.tzinfo is None or current.utcoffset() is None:
        return False
    return start.astimezone(timezone.utc) > current.astimezone(timezone.utc)


def calendar_event_matches_slot(event, slot) -> bool:
    """Bind CreateEvent wall times and timezone to the selected certified slot."""
    if not isinstance(event, dict) or not isinstance(slot, dict):
        return False
    timezone_name = str(event.get("time_zone") or "").strip()
    if timezone_name != str(slot.get("timezone") or "").strip():
        return False
    event_start = _aware_local(event.get("start"), timezone_name)
    event_end = _aware_local(event.get("end"), timezone_name)
    slot_start = _aware_local(slot.get("start"), timezone_name)
    slot_end = _aware_local(slot.get("end"), timezone_name)
    if None in (event_start, event_end, slot_start, slot_end):
        return False
    return (
        event_start.astimezone(timezone.utc)
        == slot_start.astimezone(timezone.utc)
        and event_end.astimezone(timezone.utc)
        == slot_end.astimezone(timezone.utc)
    )


def calendar_event_duration_minutes(event):
    """Return a positive whole-minute event duration, or None."""
    if not isinstance(event, dict):
        return None
    timezone_name = event.get("time_zone")
    start = _aware_local(event.get("start"), timezone_name)
    end = _aware_local(event.get("end"), timezone_name)
    if start is None or end is None or end <= start:
        return None
    seconds = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
    if seconds % 60:
        return None
    return int(seconds // 60)
