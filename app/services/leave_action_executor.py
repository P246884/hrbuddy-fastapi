"""
app/services/leave_action_executor.py
"""

import re
import json
import random
from datetime import datetime, timedelta
from dateutil import parser as dateparser

from app.api_clients.hrms_api import call_hrbuddy_api
from app.crm.entity_resolver import resolve_employee
from app.crm.crm_query_builder import build_dynamic_query
from app.crm.crm_executor import execute_crm_query
from app.intent.fast_intent import clean_text, NON_NAME_QUALIFIERS, title_name


# -------------------------------------------------------
# WEEKEND + HOLIDAY CHECK
# -------------------------------------------------------

def _get_holidays(token, user, years=None):
    """Fetch public holidays for the given year(s). Returns
    (holiday_dates:set[str], years_with_data:set[int]). A year that appears in
    `years` but returns no rows is treated as 'not configured' (caller can warn
    and fall back to weekends-only for those dates)."""
    holiday_dates = set()
    years_with_data = set()
    try:
        if not years:
            years = [datetime.now().year]
        ymin, ymax = min(years), max(years)
        query = {
            "crm_entity": "bam_holiday",
            "crm_filters": {"from_date": str(ymin) + "-01-01",
                            "to_date": str(ymax) + "-12-31"},
            "fields": {"holiday_name": "bam_name", "start_date": "bam_startdate",
                       "end_date": "bam_enddate"}
        }
        result = execute_crm_query(crm_query=query, token=token, user=user)
        if result.get("success"):
            for h in result.get("data", []):
                for key in ("start_date", "end_date"):
                    ds = str(h.get(key, ""))[:10]
                    if ds:
                        holiday_dates.add(ds)
                        try:
                            years_with_data.add(int(ds[:4]))
                        except ValueError:
                            pass
    except Exception as ex:
        print("Holiday fetch error:", ex)
    return holiday_dates, years_with_data


def _leave_day_count(from_date, to_date, token, user):
    """Count working leave days in [from_date, to_date]: always exclude
    Sat/Sun; exclude public holidays for years that HAVE a configured holiday
    list. For a year with no holidays configured (e.g. next year not added yet)
    only weekends are removed and a note is returned so the user can verify.
    Returns (count:int, note:str)."""
    try:
        fd = datetime.strptime(from_date, "%Y-%m-%d")
        td = datetime.strptime(to_date, "%Y-%m-%d")
    except Exception:
        return 0, ""
    years = list(range(fd.year, td.year + 1))
    holiday_dates, years_with_data = _get_holidays(token, user, years)
    uncovered = [y for y in years if y not in years_with_data]

    count = 0
    current = fd
    while current <= td:
        if current.weekday() < 5 and current.strftime("%Y-%m-%d") not in holiday_dates:
            count += 1
        current += timedelta(days=1)

    note = ""
    if uncovered:
        yrs = ", ".join(str(y) for y in uncovered)
        note = ("Note: public holidays for " + yrs + " aren't configured yet, "
                "so only weekends were excluded for those dates. Please verify "
                "if any holidays fall in this range.")
    return count, note


def _working_days(from_date, to_date, token, user):
    """Working-day count only (weekends + configured holidays excluded)."""
    count, _ = _leave_day_count(from_date, to_date, token, user)
    return count


# -------------------------------------------------------
# BACKDATED / PAST-DATE LEAVE RULES  (config: app/config/leave_rules.py)
# -------------------------------------------------------

def _is_past_date(date_str):
    """True if date_str (YYYY-MM-DD) is strictly before today (yesterday or
    earlier). Today itself is NOT past."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return False
    return d < datetime.now().date()


def _past_cutoff_date():
    """The earliest date a leave may start, per MAX_PAST_MONTHS. Returns a
    date, or None when the limit is disabled."""
    from app.config import leave_rules as _rules
    months = getattr(_rules, "MAX_PAST_MONTHS", None)
    if not months:
        return None
    today = datetime.now().date()
    # subtract N calendar months (handle year/month rollover + short months)
    m = today.month - months
    y = today.year
    while m <= 0:
        m += 12
        y -= 1
    day = today.day
    # clamp for months with fewer days (e.g. 31 Aug -> 30 June)
    import calendar
    last = calendar.monthrange(y, m)[1]
    if day > last:
        day = last
    return datetime(y, m, day).date()


def _check_backdated(from_date):
    """(ok, message). Blocks a start date older than the configured window."""
    from app.config import leave_rules as _rules
    cutoff = _past_cutoff_date()
    if not cutoff:
        return True, ""
    try:
        fd = datetime.strptime(from_date, "%Y-%m-%d").date()
    except Exception:
        return True, ""
    if fd < cutoff:
        msg = getattr(_rules, "OLD_DATE_MESSAGE", "")
        months = getattr(_rules, "MAX_PAST_MONTHS", 2)
        return False, msg.format(months=months, date=cutoff.isoformat())
    return True, ""


def _check_past_type(from_date, leave_type_name):
    """(ok, message). For a PAST start date, only the configured leave types
    are allowed (default: Sick Leave)."""
    from app.config import leave_rules as _rules
    allowed = getattr(_rules, "PAST_ONLY_ALLOWED_TYPES", []) or []
    if not allowed:
        return True, ""
    if not _is_past_date(from_date):
        return True, ""
    lt = (leave_type_name or "").lower()
    for a in allowed:
        if a.lower() in lt or lt in a.lower():
            return True, ""
    pretty = " or ".join(allowed)
    msg = getattr(_rules, "PAST_TYPE_MESSAGE", "")
    return False, msg.format(allowed=pretty)


def _filter_types_for_date(options, from_date):
    """Given the balance-based type-picker options and a start date, drop the
    types not permitted for a PAST date. options are strings like
    'Annual Leave (12.5 days)'. Returns the filtered list (unchanged for
    today/future or when no restriction is configured)."""
    from app.config import leave_rules as _rules
    allowed = getattr(_rules, "PAST_ONLY_ALLOWED_TYPES", []) or []
    if not allowed or not from_date or not _is_past_date(from_date):
        return options
    kept = []
    for opt in options:
        name = opt.split("(")[0].strip().lower()
        if any(a.lower() in name or name in a.lower() for a in allowed):
            kept.append(opt)
    return kept


def _check_date_validity(from_date, to_date, token, user):
    """A leave range is valid as long as it contains at least one working day.
    Weekends/holidays inside a multi-day range are simply not counted — they do
    NOT block the request. Only a selection that is ENTIRELY weekends/holidays
    is rejected."""
    try:
        fd = datetime.strptime(from_date, "%Y-%m-%d")
        td = datetime.strptime(to_date, "%Y-%m-%d")
    except Exception:
        return True, ""
    if _working_days(from_date, to_date, token, user) > 0:
        return True, ""
    # zero working days -> whole selection is weekend/holiday
    if fd.date() == td.date():
        if fd.weekday() >= 5:
            day_name = "Saturday" if fd.weekday() == 5 else "Sunday"
            return False, from_date + " is a " + day_name + ". You cannot apply leave on a weekend."
        return False, from_date + " is a public holiday. You cannot apply leave on this date."
    return False, ("The selected dates fall entirely on weekends/holidays. "
                   "Please pick working days.")


# -------------------------------------------------------
# MANAGER CHECK
# -------------------------------------------------------

def _is_manager_of(emp_name, token, user):
    """Check via permission_engine central function"""
    if not emp_name:
        return False
    try:
        from app.security.permission_engine import is_manager_of_employee
        emp_result = resolve_employee(employee_name=emp_name, token=token, user=user)
        if not emp_result.get("success") or not emp_result.get("data"):
            return False
        emp_records = emp_result.get("data", [])
        if len(emp_records) != 1:
            return False
        target_guid = emp_records[0].get("employee_guid")
        return is_manager_of_employee(user.get("user_guid", ""), target_guid, token, user)
    except Exception as e:
        print("Manager check error:", e)
    return False


# -------------------------------------------------------
# SPECIAL RESPONSE BUILDERS
# -------------------------------------------------------

def _date_picker_response(message, context, default_from="", default_to=""):
    return json.dumps({"type": "date_picker", "message": message,
                       "context": context,
                       "default_from": default_from or "",
                       "default_to": default_to or ""})

def _type_picker_response(message, options, context):
    return json.dumps({"type": "type_picker", "message": message, "options": options, "context": context})

def _reason_picker_response(message, context):
    return json.dumps({"type": "reason_picker", "message": message, "context": context})

def _leave_picker_response(message, leaves, action, context):
    return json.dumps({"type": "leave_picker", "message": message, "leaves": leaves,
                       "action": action, "page_size": 4, "context": context})

def _success_response(message):
    return json.dumps({"type": "success", "message": message})

def _error_response(message):
    return json.dumps({"type": "error", "message": message})

def _text_response(message):
    return json.dumps({"type": "text", "message": message})


# -------------------------------------------------------
# LEAVE TYPE EXTRACT
# -------------------------------------------------------

def extract_leave_type_from_message(message):
    msg = clean_text(message)
    for shortcut, lt in {"sl": "sick", "cl": "casual", "al": "annual"}.items():
        if re.search(r"\b" + shortcut + r"\b", msg):
            return lt
    for indicator in ["bimar", "tabiyat", "unwell", "not well", "feeling sick", "medical"]:
        if indicator in msg:
            return "sick"
    for lt in ["carry forward", "comp off", "compoff", "maternity", "paternity", "casual", "annual", "unpaid", "sick"]:
        if lt in msg:
            return lt
    return ""


# -------------------------------------------------------
# DATE EXTRACT
# -------------------------------------------------------

def extract_relative_date(msg):
    """Catch standalone relative dates like 'today', 'tomorrow',
    'yesterday', 'day after tomorrow' (+ common Hinglish). dateutil's
    parser cannot understand these, so we resolve them against today.
    Returns a single-day from/to (no_of_days=1) or {} if none found."""
    msg = msg.lower()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Longer phrases first so "day after tomorrow" wins over "tomorrow".
    offsets = [
        (["day after tomorrow", "day after tom",
          "parso", "parson", "parsoon", "prso", "prson", "parsoo", "parsu"], 2),
        (["day before yesterday"], -2),
        (["tomorrow", "tommorow", "tommorrow", "kal", "kl"], 1),
        (["yesterday"], -1),
        (["today", "aaj", "aj", "ajj", "aaz"], 0),
    ]

    for phrases, delta in offsets:
        for phrase in phrases:
            if re.search(r"\b" + re.escape(phrase) + r"\b", msg):
                ds = (today + timedelta(days=delta)).strftime("%Y-%m-%d")
                return {"from_date": ds, "to_date": ds, "no_of_days": 1}

    # Weekday names -> the upcoming occurrence (leave is for the future).
    # "apply leave for monday/tuesday" resolves to that day's date.
    weekdays = {
        "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
        "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
    }
    for name, wd in weekdays.items():
        if re.search(r"\b" + name + r"\b", msg):
            days_ahead = (wd - today.weekday()) % 7  # 0 = today is that weekday
            d = today + timedelta(days=days_ahead)
            ds = d.strftime("%Y-%m-%d")
            # weekday is ambiguous (this week / next) -> ask the user to confirm
            return {"from_date": ds, "to_date": ds, "no_of_days": 1,
                    "needs_confirm": True}

    return {}


def _natural_single_date(msg):
    """Parse a natural single date like '22 june 2026', '22nd june',
    'june 22 2026'. Returns single-day from/to or {}."""
    months = ("january|february|march|april|may|june|july|august|september|"
              "october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|"
              "oct|nov|dec")
    patterns = [
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:" + months + r")(?:\s+\d{4})?\b",
        r"\b(?:" + months + r")\s+\d{1,2}(?:st|nd|rd|th)?(?:\s+\d{4})?\b",
    ]
    for p in patterns:
        m = re.search(p, msg, re.IGNORECASE)
        if m:
            try:
                d = dateparser.parse(
                    m.group(0),
                    default=datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
                )
                if d:
                    ds = d.strftime("%Y-%m-%d")
                    return {"from_date": ds, "to_date": ds, "no_of_days": 1}
            except Exception:
                pass
    return {}


def extract_dates_from_message(message):
    msg = message.lower().strip()
    result = {}

    # Explicit ranges / date-pairs take priority. Only when none of those
    # structures are present do we treat a bare relative word as the date.
    has_range = bool(
        re.search(r"\bfrom\b.+\bto\b", msg)
        or re.search(r"\bse\b.+\btak\b", msg)
        or re.findall(r"\d{2}[/-]\d{2}[/-]\d{4}|\d{4}[/-]\d{2}[/-]\d{2}", msg)
    )
    if not has_range:
        rel = extract_relative_date(msg)
        if rel:
            return rel
        nat = _natural_single_date(msg)
        if nat:
            return nat

    hinglish = re.search(r"(\d{1,2}\s+\w+)\s+se\s+(\d{1,2}\s+\w+)\s+tak", msg, re.IGNORECASE)
    if hinglish:
        try:
            fd = dateparser.parse(hinglish.group(1).strip(), default=datetime.now().replace(hour=0, minute=0, second=0))
            td = dateparser.parse(hinglish.group(2).strip(), default=datetime.now().replace(hour=0, minute=0, second=0))
            if fd and td and fd.date() <= td.date():
                result["from_date"] = fd.strftime("%Y-%m-%d")
                result["to_date"] = td.strftime("%Y-%m-%d")
                result["no_of_days"] = (td.date() - fd.date()).days + 1
                return result
        except Exception:
            pass

    eng = re.search(r"from\s+(.+?)\s+to\s+(.+?)(?:\s+for|\s+reason|\s+because|$)", msg, re.IGNORECASE)
    if eng:
        try:
            fd = dateparser.parse(eng.group(1).strip(), default=datetime.now().replace(hour=0, minute=0, second=0))
            td = dateparser.parse(eng.group(2).strip(), default=datetime.now().replace(hour=0, minute=0, second=0))
            if fd and td and fd.date() <= td.date():
                result["from_date"] = fd.strftime("%Y-%m-%d")
                result["to_date"] = td.strftime("%Y-%m-%d")
                result["no_of_days"] = (td.date() - fd.date()).days + 1
                return result
            elif fd and td and fd.date() > td.date():
                result["date_error"] = True
                return result
        except Exception:
            pass

    date_pair = re.findall(r"\d{2}[/-]\d{2}[/-]\d{4}|\d{4}[/-]\d{2}[/-]\d{2}", msg)
    if len(date_pair) >= 2:
        try:
            fd = dateparser.parse(date_pair[0])
            td = dateparser.parse(date_pair[1])
            if fd and td and fd.date() <= td.date():
                result["from_date"] = fd.strftime("%Y-%m-%d")
                result["to_date"] = td.strftime("%Y-%m-%d")
                result["no_of_days"] = (td.date() - fd.date()).days + 1
                return result
            elif fd and td and fd.date() > td.date():
                result["date_error"] = True
                return result
        except Exception:
            pass

    on_match = re.search(r"\bon\s+(.+?)(?:\s+for|\s+reason|$)", msg)
    if on_match:
        try:
            single = dateparser.parse(on_match.group(1).strip(), default=datetime.now().replace(hour=0, minute=0, second=0))
            if single:
                result["from_date"] = single.strftime("%Y-%m-%d")
                result["to_date"] = single.strftime("%Y-%m-%d")
                result["no_of_days"] = 1
                return result
        except Exception:
            pass

    return result


def extract_half_day_type(message):
    msg = message.lower()
    if "first half" in msg: return "first"
    if "second half" in msg: return "second"
    return None


def _half_day_flags(message, context):
    """Work out (beginning_from, ending_in) from the message and any context.
      beginning_from='half' -> the FIRST day starts in the SECOND half (-0.5)
      ending_in='half'      -> the LAST day ends in the FIRST half   (-0.5)
    Natural language:
      'half day ... afternoon / second half'  -> begin half   (second half)
      'half day ... morning / first half'      -> end half     (first half)
      plain 'half day' (no am/pm)              -> default first half (end half)
      'beginning/start half', 'ending/end half'-> that dropdown = half
    Context markers 'start:second' / 'end:first' (from a picker) win.
    """
    # Explicit flags from the date-picker win outright (no marker translation,
    # so no chance of inversion): context carries beginning_from / ending_in.
    _cbf = (context or {}).get("beginning_from")
    _cei = (context or {}).get("ending_in")
    if _cbf in ("half", "full") and _cei in ("half", "full"):
        return _cbf, _cei

    ci = (context or {}).get("half_day_info") or ""
    beginning_from = "half" if "start:second" in ci else "full"
    ending_in = "half" if "end:first" in ci else "full"
    if ci:
        return beginning_from, ending_in

    m = clean_text(message)
    is_half = bool(re.search(r"\bhalf[\s-]?day\b|\bhalf leave\b|\baadhi?\b|"
                             r"\baadha\b|\b0\.5\b|\bhalf\b", m))

    # explicit dropdown-style phrasing
    if re.search(r"\b(begin|beginning|start|starting)\b.*\bhalf\b", m):
        beginning_from = "half"
    if re.search(r"\b(end|ending|till|until)\b.*\bhalf\b", m):
        ending_in = "half"
    if beginning_from == "half" or ending_in == "half":
        return beginning_from, ending_in

    # am/pm wording
    second = bool(re.search(r"\bafternoon\b|\bsecond half\b|\b2nd half\b|"
                            r"\bpost[\s-]?lunch\b|\bevening\b|\bdopahar\b", m))
    first = bool(re.search(r"\bmorning\b|\bfirst half\b|\b1st half\b|"
                           r"\bforenoon\b|\bpre[\s-]?lunch\b|\bsubah\b", m))
    if is_half and second:
        return "half", "full"          # second half of the (first) day
    if is_half and first:
        return "full", "half"          # first half of the (last) day
    if is_half:
        return "full", "half"          # plain half day -> first half by default
    return "full", "full"


def extract_reason_from_message(message):
    match = re.search(r"(?:reason|because|due to)\s+(.+?)(?:\s+from|\s+on|$)", message, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    msg = clean_text(message)
    action_words = {"apply", "approve", "reject", "cancel", "leave", "sick",
                    "annual", "casual", "from", "to", "for", "harshal", "purav"}
    words = msg.split()
    if len(words) <= 5 and not any(w in action_words for w in words):
        return message.strip()
    return ""


# -------------------------------------------------------
# EMPLOYEE NAME EXTRACT
# -------------------------------------------------------

def extract_employee_code_for_action(message):
    """Pull an employee code out of the message when the user disambiguates,
    e.g. 'employee code 1215', 'code IN10', 'emp no 0881', '(1216)'.
    Codes are alphanumeric (digits, or letters+digits like IN10)."""
    if not message:
        return ""
    msg = str(message)
    # "employee code 1215", "emp code IN10", "code: 0881", "employee no 1215"
    m = re.search(
        r"(?:employee\s+code|emp\s+code|employee\s+no\.?|emp\s+no\.?|code|id)\s*[:#]?\s*([A-Za-z]{0,3}\d{2,6})",
        msg, re.IGNORECASE
    )
    if m:
        return m.group(1).strip()
    # bare "(1215)" style in parentheses
    m = re.search(r"\(([A-Za-z]{0,3}\d{2,6})\)", msg)
    if m:
        return m.group(1).strip()
    return ""


def narrow_employee_records(records, message):
    """Given multiple matched employees, narrow them using ANY distinguishing
    info present in the message — not just code. Tries, in order:
      1. employee code   (exact)
      2. full name       (the message contains the whole name, e.g. "harshal patel")
      3. department      (message mentions the record's department)
      4. designation     (message mentions the record's designation)
    Returns the narrowed list (length 1 if it could disambiguate, else the
    original list unchanged)."""
    if not records or len(records) == 1:
        return records

    msg = clean_text(message)
    code = extract_employee_code_for_action(message)

    def _contains_phrase(haystack, phrase):
        # Whole-word/phrase match so "harsh" doesn't match inside "harshal".
        if not phrase:
            return False
        return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", haystack) is not None

    # Apply each available clue as a successive filter (AND). Each clue only
    # narrows when it actually distinguishes; a clue that matches everything or
    # nothing is skipped so it can't wrongly empty or collapse the list.
    def _apply(filterer):
        nonlocal records
        if len(records) <= 1:
            return
        sub = [e for e in records if filterer(e)]
        if 1 <= len(sub) < len(records):
            records = sub

    # 1) Code (most reliable) — exact match wins outright.
    if code:
        exact = [e for e in records if str(e.get("employee_code", "")).lower() == code.lower()]
        if exact:
            return exact[:1]

    # 2) Department, 3) Designation — narrow first (more specific than a name
    # prefix the user typed), then 4) name tokens as a final tiebreaker.
    _apply(lambda e: _contains_phrase(msg, clean_text(str(e.get("department", "")))))
    _apply(lambda e: _contains_phrase(msg, clean_text(str(e.get("designation", "")))))

    # 4) Name: match on any whole word of the record's name (so "harshit"
    # matches "HARSHIT SHARMA"). Only narrows when it singles out one record —
    # a bare ambiguous prefix like "harsh" that hits several stays ambiguous.
    def _name_hit(e):
        for tok in clean_text(str(e.get("employee_name", ""))).split():
            if len(tok) > 1 and _contains_phrase(msg, tok):
                return True
        return False
    _apply(_name_hit)

    return records


def extract_employee_name_for_action(message):
    msg = clean_text(message)
    # Strip the reason clause first — anything after "reason"/"because"/"kyunki"
    # is free text (e.g. "sick hu isliye") and must NOT be mined for a name.
    msg = re.split(r"\b(?:reason|because|bcoz|bcz|kyunki|kyuki|kyonki|"
                   r"cause|coz|due to)\b", msg, 1)[0].strip()
    skip = {
        "sick", "annual", "casual", "leave", "comp", "carry",
        # relative-date words (must never be read as a person's name)
        "today", "tomorrow", "yesterday",
        "parso", "parson", "parsoon", "prso", "prson", "parsoo", "parsu",
        "kal", "kl", "aaj", "aj", "ajj", "aaz", "narso",
        # weekday names (+ abbreviations) — "for wednesday" is a date, not a name
        "monday", "mon", "tuesday", "tue", "tues", "wednesday", "wed",
        "thursday", "thu", "thurs", "friday", "fri", "saturday", "sat",
        "sunday", "sun",
        # self pronouns (English + Hinglish)
        "me", "my", "i", "mere", "meri", "mera", "mujhe",
        "apni", "apna", "khud", "self",
        # third-person pronouns — "for him/her/them" is NOT a name
        "him", "her", "them", "his", "their", "he", "she", "they", "it",
        # common sentence filler that is never a name
        "city", "out", "office", "work", "home", "town", "station",
        "please", "kindly", "so", "as", "was", "were", "is", "am", "are",
        "the", "a", "an", "and", "because", "since", "today",
        # generic date/relative words — "for other/another/next day" is a DATE
        # reference, not a person's name
        "other", "another", "next", "prev", "previous", "last", "some",
        "day", "days", "date", "dates", "week", "weeks", "month", "months",
        "year", "years", "any", "new", "different", "coming", "upcoming",
        "future", "past", "later", "same", "current",
        "approval", "approvals", "approve", "approved", "pending", "requested",
        "rejected", "rejection", "cancelled", "cancellation", "awaiting", "status",
    }

    def _clean_name(name):
        """Drop relative/self/stop words from a captured name.
        'purav tomorrow' -> 'purav'; 'mere parso' -> '' (self);
        'him' -> '' (pronoun, not a name)."""
        toks = [w for w in name.split() if w not in skip and w not in NON_NAME_QUALIFIERS]
        return " ".join(toks).strip()

    # 1) "for <name>" — but if it resolves to a pronoun/filler (e.g. "for him"),
    #    fall through so we can find the real subject name below.
    match = re.search(r"\bfor\s+([a-z]+(?:\s+[a-z]+)?)(?:\s+from|\s+on|\s+sick|\s+annual|\s+casual|\s+leave|$)", msg)
    if match:
        name = _clean_name(match.group(1).strip())
        if name:
            return name.title()

    # 2) "<name> ki leave"
    match2 = re.search(r"([a-z]+(?:\s+[a-z]+)?)\s+ki\s+leave", msg)
    if match2:
        name = _clean_name(match2.group(1).strip())
        if name:
            return name.title()

    # 3) possessive "<name>'s leave"
    match3 = re.search(r"([a-zA-Z]+)'s\s+leave", message, re.IGNORECASE)
    if match3:
        name = _clean_name(clean_text(match3.group(1)))
        if name:
            return name.title()

    # 4) "of <name>" at end
    match4 = re.search(r"\bof\s+([a-zA-Z]+)(?:'s)?\s*$", message, re.IGNORECASE)
    if match4:
        name = _clean_name(clean_text(match4.group(1)))
        if name:
            return name.title()

    # 4b) "<verb> <name> leave(s)" — name sits directly before the leave noun,
    #     e.g. "approve harsh leave", "reject harshal leaves", "cancel purav
    #     leave". Skip articles/pronouns ("approve a leave", "approve my leave").
    match4b = re.search(
        r"\b(?:approve|reject|decline|deny|cancel|withdraw|grant|apply)\s+"
        r"([a-z]+(?:\s+[a-z]+)?)\s+(?:leave|leaves|chutti|chhutti)\b",
        msg
    )
    if match4b:
        name = _clean_name(match4b.group(1).strip())
        if name:
            return name.title()

    # 4c) "<verb> <name> ..." — name is the first meaningful word right after an
    #     action verb, even when a clue follows ("approve harsh with code 1215
    #     leave", "reject harsh from sales"). Stops at code/clue/filler words.
    match4c = re.search(
        r"\b(?:approve|reject|decline|deny|cancel|withdraw|grant|apply)\s+([a-z]+)",
        msg
    )
    if match4c:
        cand = match4c.group(1).strip()
        _STOP4C = {"a", "an", "the", "my", "his", "her", "their", "leave",
                   "leaves", "chutti", "chhutti", "with", "code", "for", "of",
                   # Hinglish apply-verb fragments — never a name
                   "krdo", "kardo", "kar", "krni", "karni", "krna", "karna",
                   "kr", "lagao", "lgao", "laga", "lga", "chahiye", "chaiye",
                   "do", "dedo", "kardena", "krdena"}
        if (cand not in _STOP4C and cand not in skip
                and cand not in NON_NAME_QUALIFIERS and len(cand) > 1):
            return cand.title()

    # 5) Subject-name fallback for natural sentences with a 3rd-person pronoun,
    #    e.g. "as harshal was out of city yesterday please apply leave for him".
    #    The real name is the first meaningful (non-skip) token in the message.
    if re.search(r"\b(him|her|them|his|their|he|she|they)\b", msg):
        for tok in msg.split():
            if tok in skip:
                continue
            if tok.isdigit() or len(tok) < 2:
                continue
            # ignore obvious command verbs
            if tok in ("apply", "applying", "grant", "approve", "reject",
                       "cancel", "show", "get", "fetch", "give"):
                continue
            return tok.title()

    return ""


# -------------------------------------------------------
# LEAVE BALANCE FETCH
# -------------------------------------------------------

def get_employee_leave_balances(employee_guid, token, user):
    query = build_dynamic_query(
        entity_name="leave",
        filters={"target": "employee", "employee_guid": employee_guid},
        current_user=user
    )
    result = execute_crm_query(crm_query=query, token=token, user=user)
    if not result.get("success"):
        return []
    return result.get("data", [])


def resolve_leave_type(leave_type_name, employee_guid, token, user):
    if not leave_type_name:
        return None
    records = get_employee_leave_balances(employee_guid, token, user)
    name_clean = clean_text(leave_type_name)
    print("LEAVE RECORDS:", records)
    print("LOOKING FOR:", name_clean)
    for record in records:
        lt = clean_text(str(record.get("leave_type", "")))
        if name_clean in lt or lt in name_clean:
            return {
                "leave_type_name": record.get("leave_type"),
                "balance": float(record.get("balance") or 0),
                "leave_type_guid": str(record.get("leave_type_guid") or ""),
                "leave_structure_guid": str(record.get("leave_structure_guid") or ""),
            }
    return None


# -------------------------------------------------------
# APPLY LEAVE
# -------------------------------------------------------

def handle_apply_leave(message, user, token, pending_context=None):
    context = pending_context or {}

    leave_type_name = context.get("leave_type_name") or extract_leave_type_from_message(message)
    dates = extract_dates_from_message(message)

    if dates.get("date_error"):
        return _error_response("Start date cannot be after end date. Please select correct dates."), None

    from_date = context.get("from_date") or dates.get("from_date")
    to_date = context.get("to_date") or dates.get("to_date")

    if context.get("no_of_days"):
        no_of_days = context.get("no_of_days")
    else:
        no_of_days = dates.get("no_of_days", 0)

    # Half-day handling. Flags may come from the message (NL, first turn) OR
    # from context (the date-picker dropdowns set half_day_info later). Compute
    # the EFFECTIVE flags every turn so half/half is blocked no matter the
    # source, and persist NL-derived flags into context for the picker steps.
    _bf, _ei = _half_day_flags(message, context)
    if "half_day_info" not in context:
        _markers = []
        if _bf == "half":
            _markers.append("start:second")
        if _ei == "half":
            _markers.append("end:first")
        context["half_day_info"] = ",".join(_markers)
        if (_bf == "half" or _ei == "half") and no_of_days:
            _adj = float(no_of_days) - (0.5 if _bf == "half" else 0) \
                   - (0.5 if _ei == "half" else 0)
            no_of_days = max(_adj, 0.5)
            context["no_of_days"] = no_of_days
    # half on BOTH ends -> not a continuous leave (same OR different dates).
    # Applies whether the halves came from NL or the date-picker dropdowns.
    if _bf == "half" and _ei == "half":
        return _error_response(random.choice([
            "I can't apply a leave that's a half day on both ends — there's no "
            "continuous full period. Please apply them as separate leaves (one "
            "for each half day).",
            "A half-day start AND a half-day end can't go in one leave since the "
            "period isn't continuous. Kindly raise separate leaves for each half.",
            "Half day at both the start and the end can't be applied together — "
            "please apply each half day as its own leave.",
        ])), None

    reason = context.get("reason") or extract_reason_from_message(message)
    employee_guid = context.get("employee_guid", "")
    employee_display_name = context.get("employee_display_name", "")
    is_hr = user.get("is_hr") or user.get("is_admin")

    if not employee_guid:
        emp_name_from_msg = extract_employee_name_for_action(message)
        emp_code_from_msg = extract_employee_code_for_action(message)

        if emp_name_from_msg and not is_hr and not _is_manager_of(emp_name_from_msg, token, user):
            return _error_response("You are not authorized to apply leave for other employees."), None

        if (emp_name_from_msg or emp_code_from_msg) and (is_hr or _is_manager_of(emp_name_from_msg, token, user)):
            emp_result = resolve_employee(
                employee_name=emp_name_from_msg, token=token, user=user,
                employee_code=emp_code_from_msg
            )
            if not emp_result.get("success"):
                return _error_response("Could not find employee '" + (emp_code_from_msg or emp_name_from_msg) + "'."), None
            emp_records = emp_result.get("data", [])
            if not emp_records:
                return _error_response("No employee found matching '" + (emp_code_from_msg or emp_name_from_msg) + "'."), None
            # Narrow a multi-match list by ANY distinguishing info in the message:
            # employee code, full name, department, or designation.
            if len(emp_records) > 1:
                emp_records = narrow_employee_records(emp_records, message)
            if len(emp_records) > 1:
                lines = "Multiple employees found:\n"
                for i, e in enumerate(emp_records, 1):
                    parts = [str(e.get("employee_name"))]
                    if e.get("employee_code"):
                        parts.append(str(e.get("employee_code")))
                    if e.get("department"):
                        parts.append(str(e.get("department")))
                    if e.get("designation"):
                        parts.append(str(e.get("designation")))
                    lines += str(i) + ". " + " | ".join(parts) + "\n"
                lines += ("\nPlease narrow it down — add the full name, employee "
                          "code, department, or designation. For example: "
                          "\"apply leave for " + str(emp_records[0].get("employee_code")) + "\".")
                return _text_response(lines), None
            employee_guid = emp_records[0].get("employee_guid")
            employee_display_name = emp_records[0].get("employee_name")
        else:
            employee_guid = user.get("user_guid", "")
            employee_display_name = user.get("name", "You")

    leave_options = []
    if employee_guid:
        records = get_employee_leave_balances(employee_guid, token, user)
        leave_options = [
            r.get("leave_type") + " (" + str(r.get("balance", 0)) + " days)"
            for r in records
            if r.get("leave_type") and float(r.get("balance") or 0) > 0
        ]

    # Past-date restriction: if the chosen start date is in the past, only the
    # configured leave types (default: Sick Leave) may be applied — so the
    # type picker shows only those. today/future are unaffected.
    if from_date:
        leave_options = _filter_types_for_date(leave_options, from_date)

    # Weekday-resolved date -> confirm with a PRE-FILLED date picker. The user
    # said e.g. "monday"; we show the actual resolved date so they confirm or
    # adjust (this/next week is ambiguous). Explicit dates ("22 june 2026") and
    # today/tomorrow skip this — they are unambiguous. Fires only on the first
    # turn; once the picker is submitted the date lives in context.
    if dates.get("needs_confirm") and not context.get("from_date"):
        new_ctx = {**context, "action": "apply_leave",
                   "leave_type_name": leave_type_name,
                   "employee_guid": employee_guid,
                   "employee_display_name": employee_display_name}
        return _date_picker_response(
            message=("Please confirm the date for " + employee_display_name
                     + " (we read it as " + str(from_date) + "). "
                     "Change it below if needed:"),
            context=new_ctx,
            default_from=from_date,
            default_to=to_date or from_date,
        ), new_ctx

    if not leave_type_name and not from_date:
        new_ctx = {**context, "action": "apply_leave", "employee_guid": employee_guid, "employee_display_name": employee_display_name}
        if not leave_options:
            return _error_response("No leave types found for " + employee_display_name + "."), None
        return _type_picker_response(
            message="Select leave type for " + employee_display_name + ":",
            options=leave_options,
            context={**new_ctx, "next_step": "date_picker"}
        ), new_ctx

    if not leave_type_name:
        new_ctx = {**context, "action": "apply_leave", "from_date": from_date, "to_date": to_date, "no_of_days": no_of_days, "employee_guid": employee_guid, "employee_display_name": employee_display_name}
        if not leave_options:
            return _error_response("No leave types found for " + employee_display_name + "."), None
        return _type_picker_response(
            message="Select leave type for " + employee_display_name + ":",
            options=leave_options,
            context=new_ctx
        ), new_ctx

    if not from_date or not to_date:
        new_ctx = {**context, "action": "apply_leave", "leave_type_name": leave_type_name, "from_date": from_date, "to_date": to_date, "employee_guid": employee_guid, "employee_display_name": employee_display_name}
        return _date_picker_response(
            message="Select dates for " + employee_display_name + "'s " + leave_type_name + " leave:",
            context=new_ctx
        ), new_ctx

    # EARLY date-policy gate: as soon as we know the start date (and type),
    # enforce the backdated window and past-date type rule — BEFORE asking for
    # a reason, so an old/invalid date is rejected immediately.
    ok_back, back_msg = _check_backdated(from_date)
    if not ok_back:
        return _error_response("\u274c " + back_msg), None
    ok_type, type_msg = _check_past_type(from_date, leave_type_name)
    if not ok_type:
        return _error_response("\u274c " + type_msg), None

    if not reason and not context.get("reason_asked"):
        new_ctx = {
            **context,
            "action": "apply_leave",
            "leave_type_name": leave_type_name,
            "from_date": from_date,
            "to_date": to_date,
            "no_of_days": no_of_days,
            "employee_guid": employee_guid,
            "employee_display_name": employee_display_name,
            "reason_asked": True
        }
        return _reason_picker_response(
            message="Please provide a reason for " + employee_display_name + "'s " + leave_type_name + " leave (" + str(from_date) + " to " + str(to_date) + "):",
            context=new_ctx
        ), new_ctx

    leave_info = resolve_leave_type(leave_type_name=leave_type_name, employee_guid=employee_guid, token=token, user=user)

    if not leave_info:
        clean_ctx = {
            "action": "apply_leave",
            "from_date": from_date,
            "to_date": to_date,
            "no_of_days": no_of_days,
            "reason": reason,
            "reason_asked": context.get("reason_asked", False),
            "employee_guid": employee_guid,
            "employee_display_name": employee_display_name
        }
        return _type_picker_response(
            message="Please select a valid leave type for " + employee_display_name + ":",
            options=leave_options or ["Sick Leave", "Annual Leave", "Casual Leave"],
            context=clean_ctx
        ), clean_ctx

    balance = leave_info.get("balance", 0)
    holiday_note = ""

    # Authoritative day count: working days for a multi-day range, else 1 —
    # then the half-day deductions. Flags come from context (persisted from the
    # first turn) so they survive the picker steps.
    #   half/half is already blocked at the top of this function.
    #   8->9 full/full=2, half/full=1.5, full/half=1.5 ; single any-half=0.5.
    beginning_from, ending_in = _half_day_flags(message, context)

    if from_date and to_date and from_date != to_date:
        wd, holiday_note = _leave_day_count(from_date, to_date, token, user)
        base = float(wd) if wd > 0 else 1.0
    else:
        base = 1.0

    requested = base
    if beginning_from == "half":
        requested -= 0.5
    if ending_in == "half":
        requested -= 0.5
    if (beginning_from == "half" or ending_in == "half") and requested < 0.5:
        requested = 0.5
    no_of_days = requested

    if balance < requested:
        return _error_response(
            "Insufficient " + leave_info["leave_type_name"] + " balance for " + employee_display_name + ".\n"
            "Available: " + str(balance) + " days | Requested: " + str(requested) + " working day(s)."
        ), None

    # Backdated window: block a start date older than the configured limit
    # (app/config/leave_rules.py -> MAX_PAST_MONTHS).
    ok_back, back_msg = _check_backdated(from_date)
    if not ok_back:
        return _error_response("\u274c " + back_msg), None

    # Past-date type rule: past start dates allow only the configured types
    # (default: Sick Leave). Enforced on the backend too, not just the picker.
    ok_type, type_msg = _check_past_type(from_date, leave_type_name)
    if not ok_type:
        return _error_response("\u274c " + type_msg), None

    # Weekend + Holiday check
    is_valid, date_error_msg = _check_date_validity(from_date, to_date, token, user)
    if not is_valid:
        return _error_response("\u274c " + date_error_msg), None

    # Reason field carries ONLY the user's reason. Half-day info is stored
    # properly in the CRM OptionSet fields (bam_beginningfrom / bam_endingin),
    # so we no longer prepend "Start: Second Half - ..." into the reason text.
    full_reason = reason or ""

    # Optional attachment (PNG/JPG/PDF, <=5MB) -> stored by .NET in the CRM
    # 'annotation' (Notes) entity against the leave. Validate defensively.
    _att = context.get("attachment") if isinstance(context, dict) else None
    print("APPLY ATTACHMENT: context has attachment =", bool(_att),
          "| context keys =", list(context.keys()) if isinstance(context, dict) else type(context),
          "| filename =", (_att.get("filename") if isinstance(_att, dict) else None),
          "| data_len =", (len(_att.get("data", "")) if isinstance(_att, dict) else 0))
    attachment_payload = None
    if isinstance(_att, dict) and _att.get("data"):
        _fn = str(_att.get("filename", "attachment"))
        _mt = str(_att.get("mimetype", ""))
        _ok_mt = _mt in ("image/png", "image/jpeg", "application/pdf")
        _ok_ext = bool(re.search(r"\.(png|jpe?g|pdf)$", _fn, re.IGNORECASE))
        # base64 length * 3/4 ~= byte size; 5 MB cap
        _size = int(len(_att["data"]) * 3 / 4)
        print("APPLY ATTACHMENT: ok_mt =", _ok_mt, "ok_ext =", _ok_ext,
              "size_bytes =", _size)
        if (_ok_mt or _ok_ext) and _size <= 5 * 1024 * 1024:
            attachment_payload = {
                "filename": _fn,
                "mimetype": _mt or ("application/pdf" if _fn.lower().endswith(".pdf")
                                    else "image/png" if _fn.lower().endswith(".png")
                                    else "image/jpeg"),
                "documentbody": _att["data"],   # raw base64 (no data: prefix)
            }
            print("APPLY ATTACHMENT: WILL SEND to .NET, filename =", _fn)
        else:
            print("APPLY ATTACHMENT: DROPPED (type/size invalid)")
        # invalid attachments are silently dropped — the leave still applies.

    _payload = {
        "employee_guid": employee_guid,
        "leave_type_guid": leave_info.get("leave_type_guid", ""),
        "leave_structure_guid": leave_info.get("leave_structure_guid", ""),
        "from_date": from_date,
        "to_date": to_date,
        "no_of_days": requested,
        "reason": full_reason,
        "beginning_from": beginning_from,
        "ending_in": ending_in,
    }
    if attachment_payload:
        _payload["attachment"] = attachment_payload

    response = call_hrbuddy_api(
        endpoint="/api/hrbuddy/execute-action",
        token=token, user=user, method="POST",
        body={"action": "apply_leave", "payload": _payload}
    )

    if not response.get("success"):
        return _error_response(response.get("message", "Failed to apply leave.")), None

    remaining = balance - requested
    return _success_response(
        "\u2705 " + leave_info["leave_type_name"] + " applied for " + employee_display_name + "\n"
        "\U0001f4c5 " + str(from_date) + " \u2192 " + str(to_date) + " (" + str(requested) + " day(s))\n"
        "\U0001f4dd Reason: " + str(reason or "N/A") + "\n"
        "\U0001f4ca Remaining balance: " + str(remaining) + " days"
        + (("\n\u26a0\ufe0f " + holiday_note) if holiday_note else "")
    ), None


# -------------------------------------------------------
# FETCH RECENT LEAVES
# -------------------------------------------------------

_MONTHS_FULL = ["january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december"]
_MON_SHORT = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
              "oct", "nov", "dec"]


def _specific_date(message):
    """Extract a SINGLE explicit date the user named ('6th July', 'July 6',
    '6 jul', '06/07/2026', '2026-07-06') -> 'YYYY-MM-DD'. Returns '' if the
    message names only a month/relative window (handled elsewhere)."""
    m = clean_text(message)
    yr = datetime.today().year

    def _mo(tok):
        tok = tok[:3]
        return _MON_SHORT.index(tok) + 1 if tok in _MON_SHORT else None

    # 2026-07-06
    g = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", m)
    if g:
        return f"{int(g.group(1)):04d}-{int(g.group(2)):02d}-{int(g.group(3)):02d}"
    # 06/07/2026 or 6/7/26  (assume D/M/Y — Indian format)
    g = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", m)
    if g:
        y = int(g.group(3)); y = y + 2000 if y < 100 else y
        return f"{y:04d}-{int(g.group(2)):02d}-{int(g.group(1)):02d}"
    # "6th july" / "6 july"
    g = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\b", m)
    if g and _mo(g.group(2)):
        return f"{yr:04d}-{_mo(g.group(2)):02d}-{int(g.group(1)):02d}"
    # "july 6" / "july 6th"
    g = re.search(r"\b([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b", m)
    if g and _mo(g.group(1)):
        return f"{yr:04d}-{_mo(g.group(1)):02d}-{int(g.group(2)):02d}"
    return ""


def _team_action_picker(message, user, token, action, status_filter):
    """Build a single approve/reject picker containing the pending leaves of
    ALL the manager's team members. Returns a picker response, or None if the
    user has no team / no pending leaves (caller then falls back)."""
    try:
        from app.services.comparison import _employees_in_scope
    except Exception:
        return None

    emps, _ = _employees_in_scope(user, token, force_team=True)
    if not emps:
        return None

    _STATUS = {"requested": 1, "approved": 100010001,
               "cancelled": 100010003, "rejected": 100010004,
               "cancel_request": 810100008}
    _LABEL = {1: "Requested", 100010001: "Approved",
              100010003: "Cancelled", 100010004: "Rejected",
              810100008: "Cancel Request"}
    want = status_filter if isinstance(status_filter, (list, tuple)) else [status_filter]
    want_codes = [_STATUS.get(s, 1) for s in want]

    options = []
    for e in emps:
        g = e.get("employee_guid")
        nm = e.get("employee_name", "")
        if not g:
            continue
        for code in want_codes:
            _slabel = _LABEL.get(code, "").lower().replace(" ", "_")
            q = build_dynamic_query(
                entity_name="leave_history",
                filters={"target": "employee", "employee_guid": g,
                         "status_code": code, "status": _slabel, "top": "50"},
                current_user=user)
            data = execute_crm_query(crm_query=q, token=token, user=user)
            for lv in (data.get("data", []) if data.get("success") else []):
                fd = str(lv.get("from_date", ""))[:10]
                td = str(lv.get("to_date", ""))[:10] or fd
                days = lv.get("days", "")
                span = fd if fd == td else (fd + " \u2192 " + td)
                sw = _LABEL.get(int(lv.get("status")) if isinstance(lv.get("status"), (int, float)) else 0, "")
                label = (nm + " — " + str(lv.get("leave_type", "Leave"))
                         + " | " + span + " (" + str(days) + " days)"
                         + (" | " + sw if sw else ""))
                options.append({"label": label,
                                "leave_guid": lv.get("leave_guid", "")})

    if not options:
        return None

    verb = ("approve" if action == "approve_leave"
            else "reject" if action == "reject_leave"
            else "approve cancellation of" if action == "approve_cancel"
            else "cancel")
    _pick_msg = ("Select a cancellation request to approve (your team):"
                 if action == "approve_cancel"
                 else "Select a leave to " + verb + " (your team):")
    return _leave_picker_response(
        _pick_msg,
        options, action, {}), None


def _fetch_recent_leaves_of_employee(message, token, user, action, status_filter="requested"):
    emp_name = extract_employee_name_for_action(message)
    emp_code = extract_employee_code_for_action(message)

    if not emp_name and not emp_code:
        # No employee named. For an APPROVE/REJECT action by a manager, this
        # means "show me everything I can act on" -> gather the manager's whole
        # TEAM's pending leaves into one picker (not the manager's own leaves).
        if action in ("approve_leave", "reject_leave") and \
                not (user.get("is_hr") or user.get("is_admin")):
            team_picker = _team_action_picker(message, user, token, action,
                                              status_filter)
            if team_picker is not None:
                return team_picker
        employee_guid = user.get("user_guid")
        employee_name = user.get("name", "You")
    else:
        emp_result = resolve_employee(
            employee_name=emp_name, token=token, user=user, employee_code=emp_code
        )
        if not emp_result.get("success") or not emp_result.get("data"):
            return _error_response("No employee found matching '" + (emp_code or emp_name) + "'."), None
        emp_records = emp_result.get("data", [])
        # Narrow a multi-match list by ANY clue in the message (code, full name,
        # department, designation) — same as the apply flow.
        if len(emp_records) > 1:
            emp_records = narrow_employee_records(emp_records, message)
        if len(emp_records) > 1:
            lines = "Multiple employees found:\n"
            for i, e in enumerate(emp_records, 1):
                parts = [str(e.get("employee_name"))]
                if e.get("employee_code"):
                    parts.append(str(e.get("employee_code")))
                if e.get("department"):
                    parts.append(str(e.get("department")))
                if e.get("designation"):
                    parts.append(str(e.get("designation")))
                lines += str(i) + ". " + " | ".join(parts) + "\n"
            lines += ("\nPlease narrow it down — add the full name, employee "
                      "code, department, or designation.")
            return _text_response(lines), None
        employee_guid = emp_records[0].get("employee_guid")
        employee_name = emp_records[0].get("employee_name")

    # status_filter may be a single status ("requested") or a list of statuses
    # (cancel accepts both "requested" and "approved"). Fetch each and merge,
    # de-duplicating by leave_guid.
    statuses = status_filter if isinstance(status_filter, (list, tuple)) else [status_filter]
    leaves = []
    _seen_guids = set()
    for _st in statuses:
        q = build_dynamic_query(
            entity_name="leave_history",
            filters={"target": "employee", "employee_guid": employee_guid,
                     "status": _st, "top": "50"},
            current_user=user
        )
        r = execute_crm_query(crm_query=q, token=token, user=user)
        if r.get("success") and r.get("data"):
            for lv in r["data"]:
                g = lv.get("leave_guid", "")
                if g and g in _seen_guids:
                    continue
                _seen_guids.add(g)
                leaves.append(lv)

    # For CANCEL: you may only cancel a leave whose date is today or in the
    # future — a leave whose end date has already passed can't be cancelled.
    if action == "cancel_leave":
        _today = datetime.now().date().isoformat()
        future_leaves = []
        for lv in leaves:
            end = (str(lv.get("to_date", "")) or str(lv.get("from_date", "")))[:10]
            if end and end >= _today:
                future_leaves.append(lv)
        leaves = future_leaves

    if not leaves:
        _label = "/".join(statuses)
        if action == "cancel_leave":
            return _error_response(
                "No upcoming " + _label + " leave found for "
                + str(employee_name) + " that can be cancelled. "
                "(Past-dated leaves can't be cancelled.)"), None
        return _error_response("No " + _label + " leave requests found for "
                               + str(employee_name) + "."), None

    action_label = ("Reject" if action == "reject_leave"
                    else "Approve" if action == "approve_leave" else "Cancel")
    _verb = action_label.lower()

    # map int statuscode -> readable word for the picker label
    _STATUS_LABEL = {1: "Requested", 100010001: "Approved",
                     100010003: "Cancelled", 100010004: "Rejected",
              810100008: "Cancel Request"}

    def _status_word(lv):
        s = lv.get("status")
        if isinstance(s, (int, float)):
            return _STATUS_LABEL.get(int(s), "")
        return str(s or "").title()

    def _opts(lvs):
        out = []
        for lv in lvs:
            lt = lv.get("leave_type", "Leave")
            fd = str(lv.get("from_date", ""))[:10]
            td = str(lv.get("to_date", ""))[:10]
            days = lv.get("days", "")
            sw = _status_word(lv)
            status_part = (" | " + sw) if sw else ""
            s = lv.get("status")
            scode = int(s) if isinstance(s, (int, float)) else None
            opt = {"label": f"{lt} | {fd} \u2192 {td} ({days} days){status_part}",
                   "leave_guid": lv.get("leave_guid", ""),
                   "status_code": scode}
            # For a CANCEL picker: an APPROVED leave can't be cancelled outright,
            # it must go through a cancellation REQUEST. Tag the per-item action
            # so the frontend/handler picks the right one on selection.
            if action == "cancel_leave":
                opt["action"] = ("cancel_request" if scode == 100010001
                                 else "cancel_leave")
            out.append(opt)
        return out

    def _pretty(iso):
        try:
            d = datetime.strptime(iso, "%Y-%m-%d")
            return f"{d.day} {_MON_SHORT[d.month - 1].title()} {d.year}"
        except Exception:
            return iso

    # ---- SPECIFIC DATE the user named ("6th July") ----
    target = _specific_date(message)
    if target:
        def _covers(lv):
            s = str(lv.get("from_date", ""))[:10]
            e = str(lv.get("to_date", "") or lv.get("from_date", ""))[:10]
            return bool(s) and s <= target <= (e or s)

        on_date = [lv for lv in leaves if _covers(lv)]
        pd = _pretty(target)
        if len(on_date) == 1:
            # exactly one leave on that date -> skip the picker, confirm directly
            lv = on_date[0]
            _lg = lv.get("leave_guid", "")
            # If this is a CANCEL on an APPROVED leave, it must go through a
            # cancellation REQUEST (manager approves) — not a direct cancel.
            _eff_action = action
            if action == "cancel_leave":
                _s = lv.get("status")
                _sc = int(_s) if isinstance(_s, (int, float)) else None
                if _sc == 100010001:   # Approved
                    _eff_action = "cancel_request"
            return _confirm_single_response(
                _eff_action, _lg,
                lv.get("leave_type", "Leave"),
                str(lv.get("from_date", ""))[:10],
                str(lv.get("to_date", ""))[:10],
                lv.get("days", ""),
                attachment=_attachment_for_leave(_lg, token, user)
            ), None
        if on_date:
            # more than one on that date -> let them pick
            msg = random.choice([
                f"Here are your leaves on {pd} — pick one to {_verb}:",
                f"Found a few leaves for {pd}. Select one to {_verb}:",
                f"You have multiple on {pd} — choose which to {_verb}:",
            ])
            return _leave_picker_response(message=msg, leaves=_opts(on_date),
                                          action=action, context={}), None
        # nothing on that exact date -> be clear, then show what they DO have
        if not leaves:
            return _error_response(random.choice([
                f"You have no {status_filter} leaves to {_verb} right now.",
                f"There aren't any {status_filter} leaves on your side to {_verb}.",
            ])), None
        msg = random.choice([
            f"You don't have any leave applied on {pd}. Here are your "
            f"{status_filter} leaves — pick one to {_verb}:",
            f"Nothing is on {pd}, but these are the leaves you can {_verb}:",
            f"No leave found for {pd}. Here's what you currently have — "
            f"choose one to {_verb}:",
        ])
        return _leave_picker_response(message=msg, leaves=_opts(leaves),
                                      action=action, context={}), None

    # ---- otherwise: a month/relative window ("next week", "july", "today") ----
    period_note = ""
    try:
        from app.intent.fast_intent import compute_date_range
        _fr, _to = compute_date_range(message)
    except Exception:
        _fr, _to = "", ""
    if _fr and _to:
        def _in_window(lv):
            s = str(lv.get("from_date", ""))[:10]
            e = str(lv.get("to_date", "") or lv.get("from_date", ""))[:10]
            return bool(s) and s <= _to and (e >= _fr if e else True)
        leaves = [lv for lv in leaves if _in_window(lv)]
        period_note = " in that period"

    if not leaves:
        return _error_response("No " + status_filter + " leaves found for "
                               + str(employee_name) + period_note + "."), None

    return _leave_picker_response(
        message=random.choice([
            f"Select a leave to {_verb} for {employee_name}{period_note}:",
            f"Which leave should I {_verb} for {employee_name}{period_note}?",
            f"Here are {employee_name}'s leaves{period_note} — pick one to {_verb}:",
        ]),
        leaves=_opts(leaves),
        action=action,
        context={}
    ), None


# -------------------------------------------------------
# EXECUTE ACTION BY GUID
# -------------------------------------------------------

def _execute_leave_action_by_guid(action, leave_guid, message, user, token):
    # Reason is REQUIRED for these actions (stored in CRM). If we don't have a
    # reason yet, show the reason picker first; it re-submits the same action
    # with the reason attached via pending_context.
    _REASON_ACTIONS = {"reject_leave", "cancel_leave", "cancel_request",
                       "approve_cancel"}
    if action in _REASON_ACTIONS:
        _reason = extract_reason_from_message(message)
        if not _reason:
            _prompt = {
                "reject_leave": "Please add a reason for rejecting this leave:",
                "cancel_leave": "Please add a reason for cancelling this leave:",
                "cancel_request": "Please add a reason for your cancellation "
                                  "request:",
                "approve_cancel": "Please add a comment for approving this "
                                  "cancellation:",
            }.get(action, "Please add a reason:")
            return _reason_picker_response(
                _prompt,
                {"_reason_submit": True, "action": action,
                 "leave_guid": leave_guid}), None

    if action == "approve_leave":
        response = call_hrbuddy_api(
            endpoint="/api/hrbuddy/execute-action",
            token=token, user=user, method="POST",
            body={"action": "approve_leave", "payload": {"leave_guid": leave_guid}}
        )
        if not response.get("success"):
            return _error_response(response.get("message", "Failed to approve.")), None
        return _success_response("\u2705 Leave approved successfully."), None

    elif action == "reject_leave":
        response = call_hrbuddy_api(
            endpoint="/api/hrbuddy/execute-action",
            token=token, user=user, method="POST",
            body={"action": "reject_leave", "payload": {"leave_guid": leave_guid, "reason": extract_reason_from_message(message)}}
        )
        if not response.get("success"):
            return _error_response(response.get("message", "Failed to reject.")), None
        return _success_response("\u274c Leave rejected and balance restored."), None

    elif action == "cancel_leave":
        response = call_hrbuddy_api(
            endpoint="/api/hrbuddy/execute-action",
            token=token, user=user, method="POST",
            body={"action": "cancel_leave",
                  "payload": {"leave_guid": leave_guid,
                              "comments": extract_reason_from_message(message)}}
        )
        if not response.get("success"):
            return _error_response(response.get("message", "Failed to cancel.")), None
        return _success_response("\u2705 Leave cancelled and balance restored."), None

    elif action == "cancel_request":
        # User requesting cancellation of their OWN (usually approved) leave.
        # Maps to the .NET CancelLeaveRequest -> sets status to Cancel Request
        # and emails the manager. Balance is restored only after the manager
        # approves the cancellation.
        response = call_hrbuddy_api(
            endpoint="/api/hrbuddy/execute-action",
            token=token, user=user, method="POST",
            body={"action": "cancel_request",
                  "payload": {"leave_guid": leave_guid,
                              "comments": extract_reason_from_message(message)}}
        )
        if not response.get("success"):
            return _error_response(response.get("message",
                                   "Failed to submit cancellation request.")), None
        return _success_response(
            "\u2705 Your cancellation request has been submitted to your "
            "manager for approval. You'll be notified once it's actioned."), None

    elif action == "approve_cancel":
        # Manager approving a Cancel Request -> leave becomes Cancelled and the
        # employee's balance for that leave type is restored.
        response = call_hrbuddy_api(
            endpoint="/api/hrbuddy/execute-action",
            token=token, user=user, method="POST",
            body={"action": "approve_cancel",
                  "payload": {"leave_guid": leave_guid,
                              "comments": extract_reason_from_message(message)}}
        )
        if not response.get("success"):
            return _error_response(response.get("message",
                                   "Failed to approve cancellation.")), None
        return _success_response(
            "\u2705 Cancellation approved. The leave is now cancelled and the "
            "balance has been restored to the employee."), None

    return _error_response("Unknown action."), None


# -------------------------------------------------------
# HANDLERS
# -------------------------------------------------------

def _is_any_manager(user, token):
    """True if the user manages ANYONE (has direct reports) — used for a
    general 'approve leave' with no employee named. Reuses the CRM-based check
    so a manager whose JWT lacks IsManager is still recognised."""
    if user.get("is_manager"):
        return True
    try:
        from app.services.comparison import _has_direct_reports
        return _has_direct_reports(user, token)
    except Exception:
        return False


def handle_approve_leave(message, user, token):
    leave_guid = _extract_leave_guid(message)
    # GUID directly hai — user ne leave picker se select kiya
    # Auth already check ho chuka tha leave picker dikhane se pehle
    if leave_guid:
        return _execute_leave_action_by_guid("approve_leave", leave_guid, message, user, token)

    # No GUID — check auth then show picker
    is_hr_admin = user.get("is_hr") or user.get("is_admin")
    emp_name = extract_employee_name_for_action(message)
    # authorized if: HR/admin, OR manager of the named employee, OR (no name
    # given) the user manages anyone at all.
    authorized = (is_hr_admin
                  or (emp_name and _is_manager_of(emp_name, token, user))
                  or (not emp_name and _is_any_manager(user, token)))
    if not authorized:
        return _error_response("You are not authorized to approve leaves."), None
    return _fetch_recent_leaves_of_employee(message=message, token=token, user=user, action="approve_leave")


def handle_reject_leave(message, user, token):
    leave_guid = _extract_leave_guid(message)
    if leave_guid:
        return _execute_leave_action_by_guid("reject_leave", leave_guid, message, user, token)

    is_hr_admin = user.get("is_hr") or user.get("is_admin")
    emp_name = extract_employee_name_for_action(message)
    authorized = (is_hr_admin
                  or (emp_name and _is_manager_of(emp_name, token, user))
                  or (not emp_name and _is_any_manager(user, token)))
    if not authorized:
        return _error_response("You are not authorized to reject leaves."), None
    return _fetch_recent_leaves_of_employee(message=message, token=token, user=user, action="reject_leave")


def handle_approve_cancel(message, user, token):
    """Manager approving a team member's Cancel Request. Shows the team's
    cancel-request leaves to pick from, then approves (→ Cancelled + balance
    restored via the .NET workflow)."""
    leave_guid = _extract_leave_guid(message)
    if leave_guid:
        return _execute_leave_action_by_guid("approve_cancel", leave_guid,
                                             message, user, token)
    is_hr_admin = user.get("is_hr") or user.get("is_admin")
    if not (is_hr_admin or _is_any_manager(user, token)):
        return _error_response("You are not authorized to approve "
                               "cancellation requests."), None
    # gather the team's CANCEL-REQUEST leaves into one picker
    picker = _team_action_picker(message, user, token, "approve_cancel",
                                 "cancel_request")
    if picker is not None:
        return picker
    return _text_response("There are no cancellation requests from your team "
                          "right now."), None


def handle_cancel_leave(message, user, token):
    leave_guid = _extract_leave_guid(message)
    if leave_guid:
        # A picked leave: if the user explicitly wants to cancel an APPROVED
        # leave (or said "cancel request"), submit a cancellation REQUEST
        # (manager must approve). A still-pending (requested) leave can be
        # cancelled outright.
        m = (message or "").lower()
        wants_request = bool(re.search(r"\b(cancel request|cancellation|"
                                       r"approved leave|request cancel)\b", m))
        act = "cancel_request" if wants_request else "cancel_leave"
        return _execute_leave_action_by_guid(act, leave_guid, message, user, token)

    # No guid yet -> show the user's OWN cancellable leaves to pick from.
    # requested (can cancel directly) + approved (goes to cancel-request).
    return _fetch_recent_leaves_of_employee(
        message=message, token=token, user=user,
        action="cancel_leave", status_filter=["requested", "approved"])


def handle_cancel_request(message, user, token):
    """User wants to cancel an already-APPROVED leave — this raises a Cancel
    Request that the manager must approve (per the .NET workflow)."""
    leave_guid = _extract_leave_guid(message)
    if leave_guid:
        return _execute_leave_action_by_guid("cancel_request", leave_guid,
                                             message, user, token)
    # show the user's own APPROVED leaves to pick (only approved can be
    # cancel-requested; requested ones can just be cancelled).
    return _fetch_recent_leaves_of_employee(
        message=message, token=token, user=user,
        action="cancel_request", status_filter=["approved"])


def _extract_leave_guid(message):
    match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", message, re.IGNORECASE)
    return match.group(0) if match else ""


# -------------------------------------------------------
# BULK / MULTI-PERSON ACTIONS
# "Approve vikrant, reject purav's, cancel harshal leaves",
# "approve harshal and tanish leaves", "approve harshal and reject purav", ...
# -------------------------------------------------------

_BULK_ACTION_WORDS = {
    "approve": "approve_leave", "approved": "approve_leave",
    "aprove": "approve_leave", "apprve": "approve_leave", "aprrove": "approve_leave",
    "accept": "approve_leave", "accpt": "approve_leave", "ok": "approve_leave",
    "reject": "reject_leave", "rejected": "reject_leave", "rejct": "reject_leave",
    "rejekt": "reject_leave", "decline": "reject_leave", "deny": "reject_leave",
    "cancel": "cancel_leave", "cancelled": "cancel_leave", "canceled": "cancel_leave",
    "cancl": "cancel_leave", "cancle": "cancel_leave",
}
# words that are never a person's name inside an action chunk
_BULK_SKIP = {
    "leave", "leaves", "leav", "leaves", "pending", "requested", "request",
    "the", "their", "his", "her", "its", "of", "for", "to", "please", "plz",
    "kindly", "all", "both", "last", "latest", "recent", "this", "that",
    "pls", "ki", "ka", "ke", "wali", "wala", "vala", "krdo", "kardo", "kr",
    "do", "the", "code", "with", "id", "number", "employee", "emp", "having",
}


def parse_bulk_actions(message):
    """Parse a free-form message into a list of action items:
    [{action, name, scope}]. Returns None when the message is NOT a bulk/multi
    or auto (last) action — so the normal single-action flow handles it.

    Examples that DO parse:
      "Approve vikrant, reject purav's, cancel harshal"  (3 items)
      "approve harshal and tanish leaves"                (2 items, same action)
      "approve harshal and reject purav leaves"          (2 items, 2 actions)
      "approve harshal leaves"                           (plural -> all)
      "approve last leave of harshal"                    (scope=last)
    Examples that DON'T (return None -> single flow):
      "approve harshal leave"   (singular, one person, no 'last')
      "do not approve harshal"  (negation)
    """
    raw = (message or "").lower()
    if re.search(r"\b(do not|don'?\s?t|do no|donot|never|mat|nahi)\b", raw):
        return None

    # normalise: drop possessives, turn conjunctions/separators into commas
    text = raw.replace("\u2019s", " ").replace("'s", " ")
    text = re.sub(r"\b(and|or|also|then|plus|aur|ya)\b", ",", text)
    text = text.replace("&", ",").replace("/", ",").replace(";", ",")
    chunks = [c.strip() for c in text.split(",") if c.strip()]

    items = []
    last_action = None
    for chunk in chunks:
        ctoks = re.findall(r"[a-z]+", chunk)
        code_m = re.search(r"\b(\d{3,})\b", chunk)
        code = code_m.group(1) if code_m else ""
        action = None
        name_toks = []
        for t in ctoks:
            if t in _BULK_ACTION_WORDS:
                action = _BULK_ACTION_WORDS[t]
                continue
            if t in _BULK_SKIP or t in NON_NAME_QUALIFIERS:
                continue
            if len(t) >= 2:
                name_toks.append(t)
        if action:
            last_action = action
        use_action = action or last_action
        # only approve/reject/cancel are bulk-able (apply needs dates)
        if use_action and use_action != "apply_leave" and (name_toks or code):
            item = {"action": use_action, "name": " ".join(name_toks)}
            if code:
                item["code"] = code
            items.append(item)

    # SELF bulk-cancel: "cancel all my leaves" / "cancel all leaves" / "cancel
    # my leaves" (no other person named). Users can bulk-cancel their own leaves.
    if (not items
            and re.search(r"\b(cancel|withdraw)\b|\bhata\b|\bhatao\b", raw)
            and re.search(r"\b(leave|leaves|chutti|chuttiyan|chhutiyan)\b", raw)
            and re.search(r"\ball\b|\bsaari\b|\bsaare\b|\bsare\b|\bsab\b|"
                          r"\bleaves\b|\bchuttiyan\b", raw)):
        return [{"action": "cancel_leave", "name": "", "scope": "all",
                 "self": True}]

    if not items:
        return None

    has_last = bool(re.search(r"\blast\b|\blatest\b", raw))
    has_all = bool(re.search(r"\ball\b", raw))
    plural = bool(re.search(r"\bleaves\b|\bchuttiyan\b|\bchhutiyan\b", raw))
    # Cancel is bulk-able on a plural too ("cancel purav leaves" = all of them).
    # Approve/reject keep the old rule (plural single-person -> picker), so this
    # only broadens cancel.
    cancel_plural = (all(it["action"] == "cancel_leave" for it in items)
                     and (plural or has_all))
    # Bulk = several people/actions, OR one person with explicit "all", OR a
    # plural cancel for one person.
    is_bulk = len(items) > 1 or (len(items) == 1 and (has_all or cancel_plural))
    if not is_bulk:
        return None

    scope = "last" if has_last else "all"
    for it in items:
        it["scope"] = scope
    return items


def _requested_leaves_for(employee_guid, token, user, top="50"):
    """Return the list of REQUESTED (pending) leave records for an employee,
    most-recent first (backend orders by date desc)."""
    query = build_dynamic_query(
        entity_name="leave_history",
        filters={"target": "employee", "employee_guid": employee_guid,
                 "status": "requested", "top": top},
        current_user=user,
    )
    result = execute_crm_query(crm_query=query, token=token, user=user)
    if result.get("success"):
        return result.get("data", []) or []
    return []


def _cancellable_leaves_for(employee_guid, token, user, top="50"):
    """Cancellable leaves = REQUESTED + APPROVED (the backend still rejects any
    that have already started/passed). Used by bulk cancel."""
    out = []
    for st in ("requested", "approved"):
        query = build_dynamic_query(
            entity_name="leave_history",
            filters={"target": "employee", "employee_guid": employee_guid,
                     "status": st, "top": top},
            current_user=user,
        )
        result = execute_crm_query(crm_query=query, token=token, user=user)
        if result.get("success"):
            out += (result.get("data", []) or [])
    return out


def _attachment_for_leave(leave_guid, token, user, with_body=True):
    """Return the first attachment (CRM note) for a leave, or None.
    Uses a DEDICATED .NET action ('get_leave_attachment') instead of the generic
    query — the generic query auto-adds statecode=0, which the annotation entity
    doesn't have ('Annotation entity doesn't contain attribute statecode')."""
    if not leave_guid:
        return None
    try:
        res = call_hrbuddy_api(
            endpoint="/api/hrbuddy/execute-action",
            token=token, user=user, method="POST",
            body={"action": "get_leave_attachment",
                  "payload": {"leave_guid": leave_guid}},
        )
        print("ATTACHMENT FETCH leave=", leave_guid,
              "success=", res.get("success"),
              "msg=", res.get("message"),
              "rows=", len(res.get("data", []) or []))
        rows = res.get("data", []) if res.get("success") else []
        rows = [r for r in rows if r.get("filename") or r.get("documentbody")]
        if not rows:
            return None
        r = rows[0]
        out = {
            "filename": r.get("filename") or "attachment",
            "mimetype": r.get("mimetype") or "application/octet-stream",
            "annotation_guid": r.get("annotation_guid") or "",
        }
        if with_body:
            out["documentbody"] = r.get("documentbody") or ""
        return out
    except Exception as _e:
        print("ATTACHMENT FETCH error:", _e)
        return None


def _human_date(iso):
    try:
        d = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
        return f"{d.day} {_MON_SHORT[d.month - 1].title()} {d.year}"
    except Exception:
        return str(iso)[:10]


def _confirm_single_response(action, leave_guid, leave_type, from_date,
                             to_date, days, prefix="", attachment=None):
    """Build a clean 'confirm' card for ONE specific leave. The frontend renders
    `detail` as a tidy row; `message` is a short human prompt (no pipes, no
    'yes/no' — the buttons cover that). If `attachment` is given, the card shows
    a download button."""
    verb = {"approve_leave": "approve", "reject_leave": "reject",
            "cancel_leave": "cancel"}.get(action, "process")
    fd, td = _human_date(from_date), _human_date(to_date)
    when = fd if (not td or td == fd) else f"{fd} → {td}"
    try:
        dnum = float(days)
        dtxt = (str(int(dnum)) if dnum.is_integer() else str(dnum)) + \
               (" day" if dnum == 1 else " days")
    except Exception:
        dtxt = f"{days} days"

    msg = prefix + random.choice([
        f"{verb.capitalize()} your {leave_type} on {when}?",
        f"Go ahead and {verb} your {leave_type} ({when})?",
        f"Shall I {verb} this {leave_type} for {when}?",
    ])
    label = f"{leave_type} | {str(from_date)[:10]} \u2192 {str(to_date)[:10]} ({days} days)"
    detail = {"leave_type": leave_type, "when": when, "days": dtxt, "verb": verb}
    if attachment and (attachment.get("documentbody") or attachment.get("filename")):
        detail["attachment"] = {
            "filename": attachment.get("filename", "attachment"),
            "mimetype": attachment.get("mimetype", "application/octet-stream"),
            "documentbody": attachment.get("documentbody", ""),
        }
    return json.dumps({
        "type": "confirm",
        "message": msg,
        "detail": detail,
        "context": {"_confirm": "single", "action": action,
                    "leave_guid": leave_guid, "label": label},
    })


def confirm_bulk_response(items, message, prefix=""):
    """Build a 'confirm' response that summarises a bulk action and stores it,
    so the user's next reply (yes/no/haan/naa) decides whether to run it."""
    verb_map = {"approve_leave": "approve", "reject_leave": "reject",
                "cancel_leave": "cancel"}
    parts = []
    for it in items:
        v = verb_map.get(it["action"], "process")
        scope = "the latest" if it.get("scope") == "last" else "all"
        who = "your" if (it.get("self") or not it.get("name")) else \
              title_name(it["name"]) + "'s"
        parts.append(v + " " + scope + " " + who + " leave(s)")
    summary = "; ".join(parts)
    msg = (prefix + "Just to confirm \u2014 you want me to " + summary +
           ". Shall I proceed? (yes / no)")
    return json.dumps({
        "type": "confirm",
        "message": msg,
        "context": {"_confirm": "bulk", "items": items, "message": message},
    })


def handle_bulk_action(items, message, user, token):
    """Execute parsed bulk action items and return one combined summary.
    No pickers — each named person's requested leaves are acted on directly
    (all of them, or just the most recent when scope == 'last')."""
    is_hr_admin = bool(user.get("is_hr") or user.get("is_admin"))
    verb_map = {"approve_leave": "Approved", "reject_leave": "Rejected",
                "cancel_leave": "Cancelled"}
    lines = []
    any_done = 0

    for it in items:
        action = it["action"]
        name = it["name"]
        scope = it.get("scope", "all")
        explicit_self = bool(it.get("self")) or not name
        verb = verb_map.get(action, "Processed")

        # Resolve the target first so we can tell whether "purav" is actually
        # the current user (self) before doing any permission check.
        if explicit_self:
            emp_guid = user.get("user_guid")
            emp_disp = user.get("name", "You")
        else:
            code = it.get("code", "")
            emp = resolve_employee(employee_name=name, token=token, user=user,
                                   employee_code=code) if code else \
                resolve_employee(employee_name=name, token=token, user=user)
            recs = emp.get("data", []) if emp.get("success") else []
            if len(recs) > 1 and code:
                recs = [r for r in recs if str(r.get("employee_code")) == str(code)] or recs
            if len(recs) > 1:
                recs = narrow_employee_records(recs, message)
            if not recs:
                lines.append("\u26a0\ufe0f " + title_name(name) + ": no matching employee found.")
                continue
            if len(recs) > 1:
                lines.append("\u26a0\ufe0f " + title_name(name) + ": multiple employees match — "
                             "please add the employee code (e.g. \"" + title_name(name) + " code 1234\").")
                continue
            emp_guid = recs[0].get("employee_guid")
            emp_disp = recs[0].get("employee_name") or title_name(name)

        # self = no name given OR the named person resolves to the current user
        is_self = explicit_self or (str(emp_guid) == str(user.get("user_guid")))

        # permission: acting on someone ELSE needs HR/admin or being their
        # manager. Your OWN leaves are always allowed.
        if (not is_self and action in ("approve_leave", "reject_leave", "cancel_leave")
                and not is_hr_admin and not _is_manager_of(name, token, user)):
            lines.append("\u26a0\ufe0f " + emp_disp + ": you are not authorized to "
                         + action.split("_")[0] + " their leave.")
            continue

        # cancel can act on requested + approved; approve/reject only requested.
        leaves = (_cancellable_leaves_for(emp_guid, token, user)
                  if action == "cancel_leave"
                  else _requested_leaves_for(emp_guid, token, user))
        if not leaves:
            lines.append("\u2022 " + emp_disp + ": no leaves to "
                         + action.split("_")[0] + ".")
            continue

        targets = leaves[:1] if scope == "last" else leaves
        ok = 0
        for lv in targets:
            lg = lv.get("leave_guid", "")
            if not lg:
                continue
            resp, _ = _execute_leave_action_by_guid(action, lg, message, user, token)
            try:
                if json.loads(resp).get("type") == "success":
                    ok += 1
            except Exception:
                pass
        any_done += ok
        if ok:
            suffix = " (latest)" if scope == "last" else ""
            lines.append("\u2705 " + verb + " " + str(ok) + " leave(s) for "
                         + emp_disp + suffix + ".")
        else:
            lines.append("\u26a0\ufe0f " + emp_disp + ": could not "
                         + action.split("_")[0] + " (already processed or not allowed).")

    header = ("Done \u2014 here's the summary:\n" if any_done
              else "I couldn't complete those actions:\n")
    return _text_response(header + "\n".join(lines)), None


def execute_leave_action(action, message, user, token, pending_context=None):
    if action == "apply_leave":
        return handle_apply_leave(message, user, token, pending_context)
    elif action == "approve_leave":
        return handle_approve_leave(message, user, token)
    elif action == "reject_leave":
        return handle_reject_leave(message, user, token)
    elif action == "cancel_leave":
        return handle_cancel_leave(message, user, token)
    elif action == "cancel_request":
        return handle_cancel_request(message, user, token)
    elif action == "approve_cancel":
        return handle_approve_cancel(message, user, token)
    elif action == "apply_compoff":
        from app.services.compoff_executor import build_apply_compoff
        return build_apply_compoff(message, user, token, pending_context)
    elif action in ("approve_compoff", "reject_compoff"):
        from app.services.compoff_executor import build_approve_reject_compoff
        return build_approve_reject_compoff(message, user, token, action, pending_context)
    return _text_response("I could not understand what action to perform."), None