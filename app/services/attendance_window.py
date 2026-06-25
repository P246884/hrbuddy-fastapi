"""
Attendance date-window resolver.

Attendance records exist only for the past and today — never the future. This
helper reads the user's message and returns either:

  - {"error_message": "..."}  when the asked window is entirely in the future
    (so the caller short-circuits with a friendly message instead of querying), or
  - {"date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD", "marked_by": "..."}
    with the date window (a future end is clipped to today) and, if the user
    asked for one specifically, "System Hours" or "Office Hours".

Supported phrasings: today / tomorrow / yesterday / day after-before,
Hinglish aaj/kal/parso (+ spelling variants), weekday names (monday…sunday),
weekday ranges ("monday to friday", "this week"), and explicit dates
(dd/mm/yyyy, dd-mm-yyyy, "15 june"). No date mentioned -> defaults to today.
"""

import re
import datetime

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_FUTURE_MSG = (
    "Attendance records are only available for past dates and today \u2014 "
    "future dates don't exist yet. Please ask for a past date or a weekday "
    "that has already occurred."
)


def _fmt(d):
    return d.strftime("%Y-%m-%d")


def _detect_marked_by(msg):
    if "office hour" in msg:
        return "Office Hours"
    if "system hour" in msg:
        return "System Hours"
    return ""


def _relative_single(msg, today):
    """Return a single date for relative words, or None."""
    # order matters: 2-day words before 1-day words
    if any(w in msg for w in ("day after tomorrow", "parso", "parson",
                              "parsoon", "prso", "prson")):
        return today + datetime.timedelta(days=2)
    if "day before yesterday" in msg:
        return today - datetime.timedelta(days=2)
    if "tomorrow" in msg or re.search(r"\bkal\b|\bkl\b", msg):
        return today + datetime.timedelta(days=1)
    if "yesterday" in msg:
        return today - datetime.timedelta(days=1)
    if "today" in msg or re.search(r"\baaj\b|\baj\b|\bajj\b", msg):
        return today
    return None


def _explicit_date(msg, today):
    """Parse dd/mm/yyyy, dd-mm-yyyy, or '15 june [2026]'. Returns date or None."""
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", msg)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return datetime.date(y, mo, d)
        except ValueError:
            return None
    months = ("january february march april may june july august "
              "september october november december").split()
    m2 = re.search(r"\b(\d{1,2})\s+([a-z]{3,9})\b", msg)
    if m2:
        d = int(m2.group(1))
        mon_word = m2.group(2)
        for i, name in enumerate(months, 1):
            if name.startswith(mon_word) or mon_word.startswith(name[:3]):
                try:
                    return datetime.date(today.year, i, d)
                except ValueError:
                    return None
    return None


def _weekday_range(msg, today):
    """Handle weekday ranges like 'monday to friday' / 'this week'.
    Returns (from_date, to_date) for the CURRENT week, or None."""
    if "this week" in msg:
        monday = today - datetime.timedelta(days=today.weekday())
        return monday, monday + datetime.timedelta(days=4)  # Mon..Fri
    m = re.search(r"\b(" + "|".join(_WEEKDAYS) + r")\b\s*(?:to|-|se|till|until)\s*\b("
                  + "|".join(_WEEKDAYS) + r")\b", msg)
    if m:
        monday = today - datetime.timedelta(days=today.weekday())
        a = monday + datetime.timedelta(days=_WEEKDAYS[m.group(1)])
        b = monday + datetime.timedelta(days=_WEEKDAYS[m.group(2)])
        if b < a:
            a, b = b, a
        return a, b
    return None


def _single_weekday(msg, today):
    """A lone weekday name -> that day in the current week."""
    m = re.search(r"\b(" + "|".join(_WEEKDAYS) + r")\b", msg)
    if not m:
        return None
    monday = today - datetime.timedelta(days=today.weekday())
    return monday + datetime.timedelta(days=_WEEKDAYS[m.group(1)])


def resolve_attendance_window(message, today=None):
    today = today or datetime.date.today()
    msg = (message or "").lower()

    marked_by = _detect_marked_by(msg)

    # Decide the requested window.
    rng = _weekday_range(msg, today)
    if rng:
        d_from, d_to = rng
    else:
        single = (_relative_single(msg, today)
                  or _explicit_date(msg, today)
                  or _single_weekday(msg, today))
        if single is None:
            single = today  # no date mentioned -> today
        d_from = d_to = single

    # Future guard: nothing exists beyond today.
    if d_from > today:
        return {"error_message": _FUTURE_MSG}
    if d_to > today:
        d_to = today  # clip a range that runs into the future

    result = {"date_from": _fmt(d_from), "date_to": _fmt(d_to)}
    if marked_by:
        result["marked_by"] = marked_by
    return result