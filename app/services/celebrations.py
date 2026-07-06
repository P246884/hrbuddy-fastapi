# -*- coding: utf-8 -*-
"""
Celebrations engine — company HOLIDAYS and employee BIRTHDAYS.

Holidays come from the `holiday` entity (bam_holiday) as a global, org-wide
list filtered by a date window. Birthdays are DERIVED from employees'
bam_dateofbirth (month/day match), so they need no new CRM entity.

Both return the standard "list" response the frontend already renders — no
frontend change needed. Each item: {primary, badge, fields:[[k,v],...]}.

Supported phrasings:
  Holidays  : "holidays", "holidays this month", "holidays this year",
              "next holiday" / "upcoming holiday", "how many holidays ...".
  Birthdays : "birthdays this month", "whose birthday is today",
              "birthdays in june", "next birthday" / "upcoming birthday".
"""

import json
import re
import datetime

from app.intent.fast_intent import clean_text, compute_date_range
from app.crm.crm_query_builder import build_dynamic_query
from app.crm.crm_executor import execute_crm_query

_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]
_MON_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
             "Oct", "Nov", "Dec"]


# ----------------------------------------------------------------------------
# small date helpers
# ----------------------------------------------------------------------------
def _parse_date(s):
    """Parse a CRM date string into a date. Handles ISO ('2026-05-27...') and
    US ('5/27/2026'). Returns None if unparseable."""
    if not s:
        return None
    s = str(s).strip()
    # ISO, optionally with a time. CRM date-only fields serialise as UTC
    # midnight of the LOCAL day -> for IST (+5:30) that returns the PREVIOUS
    # day at 18:30Z (e.g. 27-May comes back as '2026-05-26T18:30:00Z'). So if
    # the time is evening (>= 18:00 UTC), roll forward one day.
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", s)
    if m:
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        if m.group(4) and int(m.group(4)) >= 18:
            d = d + datetime.timedelta(days=1)
        return d
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)       # M/D/YYYY
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _fmt(d):
    if not d:
        return ""
    return f"{d.day} {_MON_ABBR[d.month - 1]} {d.year}"


def _fmt_md(d):
    if not d:
        return ""
    return f"{d.day} {_MON_ABBR[d.month - 1]}"


def _name_match(query, name):
    """Match a query token against a record name (holiday/person):
      * exact (normalized), or
      * the name STARTS WITH the query (so 'harsh' finds 'Harshal', but the
        more-specific 'harshal' does NOT match the shorter 'Harsh'), or
      * a high fuzzy ratio (>=0.87) for typos like 'bakra id' vs 'Bakrid'.
    Single-letter / <3-char tokens (initials like 'L', 'N') only match exactly,
    so they never latch onto a longer name by coincidence."""
    if not query or not name:
        return False
    import difflib
    q = re.sub(r"[^a-z0-9]", "", str(query).lower())
    n = re.sub(r"[^a-z0-9]", "", str(name).lower())
    if not q or not n:
        return False
    if q == n:
        return True
    if len(q) < 3 or len(n) < 3:
        return False
    if n.startswith(q):
        return True
    return difflib.SequenceMatcher(None, q, n).ratio() >= 0.87


def _named_month(msg):
    for i, m in enumerate(_MONTHS):
        if re.search(r"\b" + m + r"\b", msg):
            return i + 1
    # abbreviations: jan feb mar apr may jun jul aug sep(t) oct nov dec
    _abbr = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
             "sept?", "oct", "nov", "dec"]
    for i, a in enumerate(_abbr):
        if re.search(r"\b" + a + r"\b", msg):
            return i + 1
    return None


def _named_day(msg):
    """A specific day+month, e.g. '17 april', 'april 17', '17th april'."""
    mo = _named_month(msg)
    if not mo:
        return None, None
    dm = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", msg)
    return (mo, int(dm.group(1))) if dm else (mo, None)


# ----------------------------------------------------------------------------
# HOLIDAYS
# ----------------------------------------------------------------------------
_HOLIDAY_NAMES = [
    "holi", "diwali", "deepavali", "dhanteras", "dussehra", "dashera",
    "dasara", "navratri", "eid", "bakrid", "bakra id", "id ul", "ramzan",
    "ramadan", "christmas", "xmas", "rakhi", "raksha bandhan", "janmashtami",
    "krishna janmashtami", "republic day", "independence day", "gandhi jayanti",
    "new year", "makar sankranti", "sankranti", "pongal", "lohri", "baisakhi",
    "guru nanak", "gurunanak", "govardhan", "bhai dooj", "mahavir jayanti",
    "buddha purnima", "good friday", "onam", "ganesh chaturthi", "muharram",
    "ambedkar jayanti", "may day", "labour day", "ram navami",
]


def is_holiday_query(message):
    msg = clean_text(message)
    if re.search(r"\bholiday\b|\bholidays\b|\bpublic holiday|\bchutti list\b|"
                 r"\bchuttiyan\b", msg):
        return True
    # "when is <holiday name>" / "<holiday name> kab hai" — recognise common
    # holiday names even without the word 'holiday'.
    if re.search(r"\bwhen\b|\bkab\b|\bdate\b|\bkitne\b|\bkis din\b", msg):
        if any(re.search(r"\b" + re.escape(h) + r"\b", msg) for h in _HOLIDAY_NAMES):
            return True
    return False


def build_holidays(message, user, token):
    msg = clean_text(message)
    today = datetime.date.today()
    want_next = bool(re.search(r"\bnext\b|\bupcoming\b|\baane wal", msg))
    # "upcoming holiday" -> just the next one; "upcoming holidays" (plural) or
    # "all upcoming" -> every holiday from today onward.
    is_plural = bool(re.search(r"\bholidays\b", msg)) or bool(re.search(r"\ball\b", msg))

    fr = to = None
    label = ""
    if want_next:
        fr = today.isoformat()          # upcoming: startdate >= today
        label = "upcoming"
    else:
        nm = _named_month(msg)
        cr = compute_date_range(message)
        if re.search(r"\bthis month\b|\bis month\b|\bthis mnth\b", msg) or nm:
            if cr and cr[0]:
                fr, to = cr
            else:
                yr = today.year
                mo = nm or today.month
                fr = datetime.date(yr, mo, 1).isoformat()
                nxt = datetime.date(yr + (mo == 12), (mo % 12) + 1, 1)
                to = (nxt - datetime.timedelta(days=1)).isoformat()
            label = "this month" if not nm else _MONTHS[(nm or today.month) - 1].title()
        else:
            # default (and "this year") -> the whole current year
            fr = datetime.date(today.year, 1, 1).isoformat()
            to = datetime.date(today.year, 12, 31).isoformat()
            label = str(today.year)

    filters = {"target": "multiple", "date_from": fr}
    if to:
        filters["date_to"] = to
    q = build_dynamic_query(entity_name="holiday", filters=filters, current_user=user)
    data = execute_crm_query(crm_query=q, token=token, user=user)
    if not data.get("success"):
        return ("Couldn't fetch holidays right now — the CRM returned: "
                + str(data.get("message") or "unknown error")
                + " (the 'holiday' / bam_holiday entity may need to be enabled "
                "on the backend).")
    recs = data.get("data", []) or []

    # sort by start date ascending; singular "next" keeps just the soonest one
    recs.sort(key=lambda r: (_parse_date(r.get("from_date")) or datetime.date.max))

    # named holiday lookup: "when is bakrid" -> just that holiday
    name_q = next((h for h in _HOLIDAY_NAMES
                   if re.search(r"\b" + re.escape(h) + r"\b", msg)), "")
    named = False
    if name_q:
        matched = [r for r in recs if _name_match(name_q, r.get("name"))]
        if matched:
            recs = matched
            named = True
        else:
            # It's a well-known holiday (name_q comes from the common list) but
            # the ORG hasn't configured it — never invent a date.
            pretty = name_q.title()
            return (f"{pretty} is a commonly observed holiday, but it hasn't "
                    f"been set in your organization's holiday list for "
                    f"{today.year}. Only holidays configured for your "
                    f"organization are shown here.")

    if want_next and not is_plural and not named:
        recs = recs[:1]

    items = []
    for r in recs:
        sd = _parse_date(r.get("from_date"))
        ed = _parse_date(r.get("to_date"))
        if ed and sd and ed < sd:      # safety net against off-by-one
            ed = sd
        span = _fmt(sd) + ((" → " + _fmt(ed)) if ed and ed != sd else "")
        fields = []
        if span:
            fields.append(["Date", span])
        if r.get("days") not in (None, ""):
            fields.append(["Days", str(r.get("days"))])
        if r.get("business_unit"):
            fields.append(["Business Unit", str(r.get("business_unit"))])
        items.append({
            "primary": r.get("name") or "Holiday",
            "badge": "🎉 Holiday",
            "fields": fields,
        })

    if not items:
        if want_next:
            return "No upcoming holidays found."
        return f"No holidays found for {label}."

    if named:
        _sd = _parse_date(recs[0].get("from_date"))
        intro = (recs[0].get("name") or "Holiday") + " — " + (_fmt(_sd) if _sd else "date n/a")
    elif want_next and not is_plural:
        intro = "Next holiday — " + items[0]["primary"]
    elif want_next:
        intro = f"Upcoming holidays — {len(items)} found."
    else:
        intro = f"Holidays ({label}) — {len(items)} found."
    return json.dumps({
        "type": "list", "kind": "holiday", "intro": intro,
        "count": len(items), "page_size": 12, "items": items,
    })


# ----------------------------------------------------------------------------
# BIRTHDAYS  (derived from employees' bam_dateofbirth)
# ----------------------------------------------------------------------------
def is_birthday_query(message):
    msg = clean_text(message)
    return bool(re.search(r"\bbirthday\b|\bbirthdays\b|\bbday\b|\bbdays\b|"
                          r"\bb-day\b|\bborn\b|\bdob\b|date of birth", msg))


def build_birthdays(message, user, token):
    msg = clean_text(message)
    today = datetime.date.today()
    want_next = bool(re.search(r"\bnext\b|\bupcoming\b|\baane wal", msg))
    want_today = bool(re.search(r"\btoday\b|\baaj\b", msg))
    want_all = bool(re.search(r"\ball\b|\btotal\b|\bsaari\b|\bsabhi\b|\bsare\b|"
                              r"\bsab\b|\bcomplete\b|\bentire\b|\beveryone\b|"
                              r"\bwhole\b", msg))
    # "upcoming birthday" -> the single next one; "upcoming birthdays" (plural)
    # -> every upcoming birthday in order.
    is_plural = bool(re.search(r"\bbirthdays\b|\bbdays\b", msg))
    nm = _named_month(msg)
    spec_mo, spec_day = _named_day(msg)     # e.g. "17 april"

    # person-name lookup: "when is harshal's birthday", "harshal birthday",
    # "whose birthday -> <name>". Strip all the framing words; whatever is left
    # is a candidate person name (matched against the employee list below).
    _BSTRIP = {"when", "is", "was", "the", "a", "an", "birthday", "birthdays",
               "bday", "bdays", "of", "whose", "tell", "me", "show", "date",
               "dob", "born", "kab", "hai", "ki", "ka", "ke", "list", "all",
               "total", "upcoming", "next", "this", "month", "today", "aaj",
               "in", "on", "celebration", "celebrations", "kis", "kaun", "kon",
               "employee", "s", "coming", "everyone", "sab", "sabhi", "saari",
               "my", "meri", "mera", "mere", "birth", "mujhe", "bta", "batao",
               "dikhao", "please", "plz"}
    _BSTRIP |= set(_MONTHS)
    _name_tokens = [t for t in re.findall(r"[a-z]+", msg)
                    if t not in _BSTRIP and len(t) >= 3]
    person_q = " ".join(_name_tokens).strip()

    emp_q = build_dynamic_query(entity_name="employee",
                                filters={"target": "multiple"}, current_user=user)
    edata = execute_crm_query(crm_query=emp_q, token=token, user=user)
    if not edata.get("success"):
        return ("Couldn't fetch employees right now — the CRM returned: "
                + str(edata.get("message") or "unknown error") + ".")
    emps = edata.get("data", []) or []
    if not emps:
        return "I couldn't load the employee list right now."

    # attach parsed DOB
    people = []
    for e in emps:
        dob = _parse_date(e.get("date_of_birth"))
        if not dob:
            continue
        people.append((e, dob))

    def _emp_matches(e):
        nm_full = str(e.get("employee_name") or "")
        return (any(_name_match(tok, w) for tok in _name_tokens
                    for w in nm_full.split())
                or _name_match(person_q, nm_full))

    # --- named person lookup (wins over month/next/all) ---
    # Only for a PURE name query (no month/all/next/today window). Match against
    # ALL employees (incl. blank DOB) so we can say "X's DOB isn't set" for an
    # exact person while still surfacing close matches that DO have a birthday.
    if person_q and not (want_all or want_next or want_today or nm or (spec_mo and spec_day)):
        hits = [e for e in emps if _emp_matches(e)]
        if not hits:
            return f"I couldn't find an employee named '{person_q}'."
        if hits:
            with_dob = []
            no_dob = []
            for e in hits:
                d = _parse_date(e.get("date_of_birth"))
                (with_dob.append((e, d)) if d else no_dob.append(e))
            with_dob.sort(key=lambda p: (p[1].month, p[1].day))

            items = []
            for e, d in with_dob:
                fields = [["Birthday", _fmt_md(d)]]
                if e.get("department"):
                    fields.append(["Department", str(e.get("department"))])
                if e.get("designation"):
                    fields.append(["Designation", str(e.get("designation"))])
                items.append({"primary": e.get("employee_name") or "Employee",
                              "badge": "🎂 " + _fmt_md(d), "fields": fields})

            not_set = "; ".join((e.get("employee_name") or "This employee")
                                + "'s date of birth isn't set" for e in no_dob)

            if not items:
                # only blank-DOB matches
                return (not_set + ".") if not_set else \
                    f"No birthday found for '{person_q}'."

            if len(items) == 1 and not no_dob:
                intro = items[0]["primary"] + "'s birthday — " + _fmt_md(with_dob[0][1])
            else:
                head = (not_set + ". Other matches: ") if not_set else \
                       f"{len(items)} matching birthday(s): "
                intro = head + ", ".join(
                    f"{i['primary']} ({i['fields'][0][1]})" for i in items)

            return json.dumps({
                "type": "list", "kind": "birthday", "intro": intro,
                "count": len(items), "page_size": 12, "items": items,
            })
        # no employee matched at all -> fall through to generic handling

    if not people:
        return ("No dates of birth are available to compute birthdays. "
                "(Confirm the bam_dateofbirth field is being returned.)")

    label = ""
    chosen = []
    if want_today:
        chosen = [(e, d) for (e, d) in people
                  if d.month == today.month and d.day == today.day]
        label = "today"
    elif spec_mo and spec_day:
        # a specific date like "17 april"
        chosen = [(e, d) for (e, d) in people
                  if d.month == spec_mo and d.day == spec_day]
        label = f"{spec_day} {_MON_ABBR[spec_mo - 1]}"
        if not chosen:
            return f"No one has a birthday on {label}."
    elif want_next:
        # upcoming (month, day) from today, wrapping the year
        def _days_away(d):
            this_year = datetime.date(today.year, d.month, d.day) \
                if _valid(today.year, d.month, d.day) else None
            if this_year and this_year >= today:
                return (this_year - today).days
            ny = datetime.date(today.year + 1, d.month, d.day) \
                if _valid(today.year + 1, d.month, d.day) else None
            return (ny - today).days if ny else 999
        people.sort(key=lambda p: _days_away(p[1]))
        if is_plural:
            # "upcoming birthdays" -> everyone, in upcoming order
            chosen = people
            label = "upcoming"
        else:
            # "upcoming birthday" -> just the soonest (all sharing that day)
            soonest = _days_away(people[0][1])
            chosen = [p for p in people if _days_away(p[1]) == soonest]
            label = "next"
    elif want_all and not nm:
        # "all birthdays" / "total birthdays" -> everyone, by month then day
        chosen = sorted(people, key=lambda p: (p[1].month, p[1].day))
        label = "all"
    else:
        mo = nm or today.month           # default: this month
        chosen = [(e, d) for (e, d) in people if d.month == mo]
        chosen.sort(key=lambda p: p[1].day)
        label = _MONTHS[mo - 1].title()

    if not chosen:
        return f"No birthdays found for {label}."

    items = []
    for e, d in chosen:
        fields = [["Birthday", _fmt_md(d)]]
        if e.get("department"):
            fields.append(["Department", str(e.get("department"))])
        if e.get("designation"):
            fields.append(["Designation", str(e.get("designation"))])
        items.append({
            "primary": e.get("employee_name") or "Employee",
            "badge": "🎂 " + _fmt_md(d),
            "fields": fields,
        })

    if want_next and not is_plural:
        intro = "Next birthday — " + ", ".join(i["primary"] for i in items)
    elif want_next:
        intro = f"Upcoming birthdays — {len(items)} found."
    elif want_today:
        intro = f"🎂 {len(items)} birthday(s) today!"
    elif label == "all":
        intro = f"All birthdays — {len(items)} found."
    else:
        intro = f"Birthdays ({label}) — {len(items)} found."
    return json.dumps({
        "type": "list", "kind": "birthday", "intro": intro,
        "count": len(items), "page_size": 12, "items": items,
    })


def _valid(y, m, d):
    try:
        datetime.date(y, m, d)
        return True
    except ValueError:
        return False