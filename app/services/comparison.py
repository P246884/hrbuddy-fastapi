"""
Comparison engine — compare two or more employees on a metric and return a
structured response the frontend renders as a bar chart + table + export.

Metrics supported in v1:
  * leave days taken          (default; from leave_history, status-aware)
  * experience (years)        (when the query mentions experience)

Windows supported: any range compute_date_range understands
  (this/next/last week|month|year, a named month, today, "march 2025", ...).

Ranking ("who took the most / least / max / min leaves between X and Y") is the
same computation with max/min highlighted.
"""

import json
import re

from app.intent.fast_intent import clean_text, compute_date_range, NON_NAME_QUALIFIERS
from app.crm.crm_query_builder import build_dynamic_query
from app.crm.crm_executor import execute_crm_query
from app.services.dynamic_executor import can_read_entity
from app.services.leave_action_executor import resolve_employee


_COMP_EXTRA_STOP = {
    "compare", "comparison", "compared", "comparing", "leave", "leaves",
    "experience", "exp", "experienced", "took", "take", "taken", "taking",
    "days", "day", "balance", "history", "info", "details", "profile",
    "between", "amongst", "among", "from", "of", "the", "their", "show",
    "me", "us", "out", "these", "those", "both",
    # Hinglish ranking / question / grammar words — never a person's name
    "sabse", "jyada", "zyada", "jada", "kam", "adhik", "kisne", "kis", "kaun",
    "kon", "kaunsi", "konsi", "kitni", "kitne", "li", "liya", "liye", "ne",
    "ya", "aur", "hai", "hain", "wale", "wala", "wali", "sab", "saare", "sare",
    "most", "least", "more", "less", "fewer", "maximum", "minimum", "max",
    "min", "highest", "lowest", "top", "bottom", "who", "which", "whom",
}
_STOP = NON_NAME_QUALIFIERS | _COMP_EXTRA_STOP

# status codes (mirror entity_registry)
_STATUS = {"requested": 1, "approved": 100010001,
           "cancelled": 100010003, "rejected": 100010004,
           "cancel_request": 810100008}


def is_team_roster_query(message):
    """'show my team', 'my team members', 'who is in my team', 'who reports to
    me', 'list my reportees'. A manager wants the LIST of their direct reports
    (not leaves/balance — just who they are)."""
    m = clean_text(message)
    # must NOT be about leave/balance/status/cancel-request etc (those have
    # their own flows) — roster is ONLY "who is on my team".
    if re.search(r"\b(leave|leaves|balance|pending|rejected|approved|"
                 r"cancel|cancelled|cancellation|request|requests|requested|"
                 r"chhutti|chutti|salary|attendance|status)\b", m):
        return False
    has_team = bool(re.search(r"\b(my team|team members?|my reportees?|"
                              r"my reports|meri team|who reports to me|"
                              r"who.s in my team|who is in my team)\b", m))
    return has_team


def build_team_roster(message, user, token):
    """List the current user's direct reports (their team). Managers see their
    team; HR sees the whole org (with a tip to filter by department)."""
    scope = _actor_scope(user, token)
    is_hr = bool(user.get("is_hr") or user.get("is_admin"))

    if scope == "none":
        return ("You don't have any team members reporting to you. If you're a "
                "manager and this looks wrong, please check with HR.")

    emps, _ = _employees_in_scope(user, token, force_team=not is_hr)
    if not emps:
        if not is_hr:
            return "You don't have any team members reporting to you."
        return "I couldn't load the employee list right now."

    items = []
    for e in emps[:200]:
        nm = e.get("employee_name") or "Employee"
        nm = nm.title() if isinstance(nm, str) and nm.isupper() else nm
        fields = [["Dept", str(e.get("department") or "—")],
                  ["Designation", str(e.get("designation") or "—")]]
        exp = e.get("experience")
        if exp not in (None, ""):
            try:
                fields.append(["Experience", str(float(exp)) + " yrs"])
            except (TypeError, ValueError):
                pass
        code = e.get("employee_code")
        if code:
            fields.append(["Code", str(code)])
        items.append({"primary": nm, "fields": fields})

    who = "your team" if not is_hr else "the organization"
    summary = ("You have " + str(len(items)) + " team member(s)."
               if not is_hr else
               "Showing all " + str(len(items)) + " employees (you're HR).")
    return json.dumps({
        "type": "list", "kind": "employee",
        "intro": ("Your team" if not is_hr else "All employees") +
                 " — " + str(len(items)) + " " +
                 ("person" if len(items) == 1 else "people"),
        "summary": summary,
        "count": len(items), "page_size": 10, "items": items,
    })


def is_on_leave_query(message):
    """'who is on leave', 'employees on leave in january', 'who's absent today'."""
    m = clean_text(message)
    return (bool(re.search(r"\b(on leave|on vacation|absent)\b", m))
            and bool(re.search(r"\b(who|whos|employees?|people|staff|anyone|"
                               r"which|list|everyone)\b", m)))


def _specific_day(message):
    """If the message names ONE explicit calendar day (e.g. '20 july 2026',
    'july 20 2026', '20/07/2026', '2026-07-20', 'on 20 july'), return it as
    'YYYY-MM-DD'. Otherwise return None.

    compute_date_range() widens 'day month year' to the whole month, which is
    wrong for 'who is on leave ON <day>'. This narrows it back to that one day.
    Year defaults to the current year when omitted."""
    import datetime, re as _re
    m = clean_text(message)
    months = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3,
              "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
              "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
              "september": 9, "oct": 10, "october": 10, "nov": 11,
              "november": 11, "dec": 12, "december": 12}
    this_year = datetime.date.today().year

    # ISO: 2026-07-20
    iso = _re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", m)
    if iso:
        y, mo, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            return None

    # numeric: 20/07/2026 or 20-07-2026 (day-first)
    num = _re.search(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](20\d{2})\b", m)
    if num:
        d, mo, y = int(num.group(1)), int(num.group(2)), int(num.group(3))
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            return None

    # "20 july 2026" / "20 july" / "20th july 2026"
    dm = _re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
                    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
                    r"january|february|march|april|june|july|august|september|"
                    r"october|november|december)\b(?:\s+(20\d{2}))?", m)
    if not dm:
        # "july 20 2026" / "july 20th"
        dm = _re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
                        r"january|february|march|april|june|july|august|"
                        r"september|october|november|december)\s+"
                        r"(\d{1,2})(?:st|nd|rd|th)?\b(?:\s+(20\d{2}))?", m)
        if dm:
            mo = months[dm.group(1)]
            d = int(dm.group(2))
            y = int(dm.group(3)) if dm.group(3) else this_year
        else:
            return None
    else:
        d = int(dm.group(1))
        mo = months[dm.group(2)]
        y = int(dm.group(3)) if dm.group(3) else this_year

    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return None


def build_on_leave(message, user, token):
    """Scan the org for approved leaves overlapping a date window (default:
    today) and list who is on leave. HR/admin only."""
    import datetime
    if not (user.get("is_hr") or user.get("is_admin")):
        return ("Viewing who's on leave across the org is available to HR only. "
                "You can still check one person — e.g. \"show Purav's leaves next week\".")

    # A specific day ("on 20 july 2026") must stay a single day — otherwise
    # compute_date_range widens it to the whole month. Fall back to the range
    # (this/next month, etc.) only when no explicit day was named.
    day = _specific_day(message)
    if day:
        fr = to = day
    else:
        fr, to = compute_date_range(message)
        if not (fr and to):
            today = datetime.date.today().isoformat()
            fr = to = today
    period = fr if fr == to else (fr + " to " + to)

    emp_q = build_dynamic_query(entity_name="employee",
                                filters={"target": "multiple"}, current_user=user)
    edata = execute_crm_query(crm_query=emp_q, token=token, user=user)
    emps = edata.get("data", []) if edata.get("success") else []
    if not emps:
        return "I couldn't load the employee list right now."

    items = []
    for e in emps[:500]:
        guid = e.get("employee_guid")
        if not guid:
            continue
        filters = {"target": "employee", "employee_guid": guid, "top": "50",
                   "from_date": fr, "to_date": to,
                   "status": "approved", "status_code": _STATUS["approved"]}
        q = build_dynamic_query(entity_name="leave_history", filters=filters,
                                current_user=user)
        data = execute_crm_query(crm_query=q, token=token, user=user)
        recs = data.get("data", []) if data.get("success") else []
        for r in recs:
            frm = (r.get("from_date", "") or "")[:10]
            t = (r.get("to_date", "") or "")[:10]
            dates = frm + ((" → " + t) if t and t != frm else "")
            items.append({
                "primary": (e.get("employee_name") or "Employee"),
                "badge": "On leave",
                "fields": [["Type", r.get("leave_type") or "-"],
                           ["Dates", dates],
                           ["Days", str(r.get("days") or "")]],
            })

    if not items:
        return "✅ No one has approved leave for " + period + "."
    return json.dumps({
        "type": "list", "kind": "leave",
        "intro": "On leave (" + period + ") — " + str(len(items)) + " record(s)",
        "count": len(items), "page_size": 10, "items": items,
    })


def is_org_ranking_query(message):
    """True for 'who took the most/least leaves' or 'who has the most
    experience' across the WHOLE org (no specific names)."""
    m = clean_text(message)
    rank = bool(re.search(r"\b(most|maximum|max|least|minimum|min|highest|"
                          r"lowest|fewest|top)\b", m)) \
        or "most experienced" in m \
        or bool(re.search(r"\bsabse (jyada|zyada|kam|adhik)\b", m))
    subj = bool(re.search(r"\b(leave|leaves|experience|exp|experienced)\b", m))
    return rank and subj


def build_org_ranking(message, user, token):
    """Rank ALL employees by experience (cheap) or leave days taken (HR-only,
    one query per person). Returns a 'comparison' JSON or a plain string."""
    m = clean_text(message)
    by_exp = bool(re.search(r"\b(experience|exp|experienced)\b", m))
    _has_most = bool(re.search(r"\b(most|maximum|max|highest|top|jyada|zyada|adhik)\b", m))
    _has_min = bool(re.search(r"\b(least|minimum|min|lowest|fewest|kam)\b", m))
    # "most/least" together (ambiguous) -> treat as MOST (the usual intent)
    want_min = _has_min and not _has_most
    topn_m = re.search(r"\btop\s+(\d+)", m)
    topn = int(topn_m.group(1)) if topn_m else 5
    is_hr_admin = bool(user.get("is_hr") or user.get("is_admin"))

    if not by_exp and not is_hr_admin:
        return ("Org-wide leave rankings are available to HR only. You can still "
                "compare specific people — e.g. \"compare Purav and Harshal leaves\".")

    emp_filters = {"target": "multiple"}
    desig = None
    dep = None
    _skip = {"the", "of", "by", "in", "a", "an", "with", "for", "this", "their"}
    dm = re.search(r"\b([a-z]+)\s+designation\b", m) or re.search(r"\bdesignation\s+(?:of\s+)?([a-z]+)", m)
    if dm and dm.group(1) not in _skip:
        desig = dm.group(1)
    pm = re.search(r"\b([a-z]+)\s+(?:department|dept)\b", m) or re.search(r"\b(?:department|dept)\s+(?:of\s+)?([a-z]+)", m)
    if pm and pm.group(1) not in _skip:
        dep = pm.group(1)
    if desig:
        emp_filters["designation"] = desig
    if dep:
        emp_filters["department"] = dep

    emp_q = build_dynamic_query(entity_name="employee",
                                filters=emp_filters, current_user=user)
    edata = execute_crm_query(crm_query=emp_q, token=token, user=user)
    emps = edata.get("data", []) if edata.get("success") else []
    if desig:
        emps = [e for e in emps if desig.lower() in str(e.get("designation", "")).lower()] or emps
    if dep:
        emps = [e for e in emps if dep.lower() in str(e.get("department", "")).lower()] or emps
    if not emps:
        return "I couldn't find any matching employees to rank."

    scope_note = (" · " + desig.title()) if desig else (" · " + dep.title()) if dep else ""

    rows = []
    if by_exp:
        for e in emps:
            try:
                v = float(e.get("experience") or 0)
            except (TypeError, ValueError):
                v = 0.0
            rows.append((e.get("employee_name") or "Employee", v))
        metric, unit, period = "Experience (years)", "years", ""
    else:
        fr, to = compute_date_range(message)
        status_code = _STATUS["approved"]
        # full-org ranking: one leave lookup per employee (sequential). Sane
        # upper bound only to avoid a pathological loop.
        for e in emps[:500]:
            guid = e.get("employee_guid")
            if not guid:
                continue
            days, _, _ = _leave_metric(guid, user, token, status_code, fr, to)
            rows.append((e.get("employee_name") or "Employee", days))
        metric, unit = "Leave days taken (approved)", "days"
        period = (fr + " to " + to) if fr and to else ""

    both = _has_most and _has_min
    rows.sort(key=lambda x: x[1], reverse=True)  # highest first

    if both and len(rows) >= 2:
        n = topn if topn_m else 3
        n = max(1, min(n, len(rows) // 2 or 1))
        most_rows = rows[:n]
        least_rows = [r for r in rows[-n:] if r not in most_rows]
        combined = most_rows + least_rows          # already descending overall
        items = []
        for nm, v in combined:
            nm = nm.title() if isinstance(nm, str) and nm.isupper() else nm
            items.append({"name": nm, "value": _fmt(v)})
        hi_nm = items[0]["name"]
        hi_v = items[0]["value"]
        lo_nm = items[-1]["name"]
        lo_v = items[-1]["value"]
        verb = "has" if by_exp else "took"
        summary = (hi_nm + " " + verb + " the most (" + str(hi_v) + " " + unit
                   + "), " + lo_nm + " the least (" + str(lo_v) + " " + unit + ").")
        title = ("Most & least experienced" if by_exp else "Most & least leaves taken")
        return json.dumps({
            "type": "comparison",
            "title": title + scope_note,
            "metric": metric, "period": period, "unit": unit,
            "items": items, "summary": summary,
        })

    # single end (most OR least)
    if want_min:
        rows.sort(key=lambda x: x[1])  # lowest first
    top = rows[:topn]
    items = []
    for nm, v in top:
        nm = nm.title() if isinstance(nm, str) and nm.isupper() else nm
        items.append({"name": nm, "value": _fmt(v)})

    summary = ""
    if items:
        lead = items[0]
        word = ("lowest" if want_min else "highest") if by_exp else \
               ("fewest" if want_min else "most")
        summary = lead["name"] + " has the " + word + " (" + str(lead["value"]) + " " + unit + ")."

    title = ("Most experienced" if (by_exp and not want_min) else
             "Least experienced" if by_exp else
             "Fewest leaves taken" if want_min else "Most leaves taken")
    return json.dumps({
        "type": "comparison",
        "title": title + " (top " + str(len(items)) + ")" + scope_note,
        "metric": metric, "period": period, "unit": unit,
        "items": items, "summary": summary,
    })


def is_comparison_query(message):
    """True when the message asks to compare / rank a set of named people."""
    m = clean_text(message)
    if re.search(r"\b(compare|comparison|versus|vs|v/s)\b", m):
        return True
    if re.search(r"\bbetween\b", m) and re.search(r"\b(leave|leaves|experience)\b", m):
        return True
    # "who took the most/least leaves ... <names>"
    if re.search(r"\b(who|which)\b", m) \
            and re.search(r"\b(most|maximum|max|least|minimum|min|more|fewer|"
                          r"highest|lowest)\b", m) \
            and re.search(r"\b(leave|leaves|experience)\b", m):
        return True
    return False


def extract_comparison_names(message):
    """Split the message on vs / between / and / commas and clean each segment
    down to a person's name, dropping comparison/leave vocabulary."""
    text = (message or "").lower()
    # remove non-separator comparison verbs
    text = re.sub(r"\b(compare|comparison|compared|comparing)\b", " ", text)
    # split on the separators people actually use
    parts = re.split(r"\bvs\b|\bv/s\b|\bversus\b|\bbetween\b|\band\b|\bor\b|"
                     r"\baur\b|\bya\b|,|&|/|\bplus\b", text)
    names = []
    for p in parts:
        toks = [t for t in re.findall(r"[a-z]+", p)
                if t not in _STOP and len(t) > 2]
        if toks:
            nm = " ".join(toks)
            names.append(nm.title())
    # de-dup, keep order
    seen, out = set(), []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def _fmt(n):
    return int(n) if float(n).is_integer() else round(float(n), 1)


def _leave_metric(guid, user, token, status_code, fr, to):
    """Sum leave days (and count) for one employee, optionally windowed."""
    filters = {"target": "employee", "employee_guid": guid, "top": "200"}
    if status_code:
        filters["status_code"] = status_code  # builder also accepts 'status'
        filters["status"] = [k for k, v in _STATUS.items() if v == status_code][0]
    if fr and to:
        filters["from_date"] = fr
        filters["to_date"] = to
    q = build_dynamic_query(entity_name="leave_history", filters=filters, current_user=user)
    data = execute_crm_query(crm_query=q, token=token, user=user)
    recs = data.get("data", []) if data.get("success") else []
    total_days = 0.0
    by_type = {}
    for r in recs:
        try:
            d = float(r.get("days") or 0)
        except (TypeError, ValueError):
            d = 0.0
        total_days += d
        lt = r.get("leave_type") or "Other"
        by_type[lt] = by_type.get(lt, 0.0) + d
    return total_days, len(recs), by_type


def build_comparison(message, user, token):
    """Return a JSON 'comparison' response string, or a plain error string."""
    names = extract_comparison_names(message)
    if len(names) < 2:
        return ("To compare, name at least two people — e.g. "
                "\"compare Purav and Harshal leaves this year\".")

    msg = clean_text(message)
    by_experience = bool(re.search(r"\bexperience|exp\b", msg))
    fr, to = compute_date_range(message)

    # which leave status counts as "taken"? default approved; honour an
    # explicit status word; "applied/requested/pending" -> requested.
    status_code = _STATUS["approved"]
    status_label = "approved"
    if re.search(r"\b(pending|requested|applied|awaiting)\b", msg):
        status_code, status_label = _STATUS["requested"], "requested"
    elif re.search(r"\brejected\b", msg):
        status_code, status_label = _STATUS["rejected"], "rejected"
    elif re.search(r"\b(all|total|every)\b", msg):
        status_code, status_label = None, "all"

    items = []
    denied = []
    for nm in names:
        res = resolve_employee(employee_name=nm, token=token, user=user)
        recs = res.get("data", []) if res.get("success") else []
        if not recs:
            items.append({"name": nm, "value": None, "note": "not found"})
            continue
        rec = recs[0]
        guid = rec.get("employee_guid")
        disp = rec.get("employee_name") or nm
        disp = disp.title() if disp.isupper() else disp
        if not can_read_entity(entity="leave_history", current_user=user,
                               target_employee=guid, token=token):
            denied.append(disp)
            items.append({"name": disp, "value": None, "note": "not authorized"})
            continue
        if by_experience:
            try:
                val = float(rec.get("experience") or 0)
            except (TypeError, ValueError):
                val = 0.0
            items.append({"name": disp, "value": _fmt(val)})
        else:
            days, cnt, by_type = _leave_metric(guid, user, token, status_code, fr, to)
            items.append({"name": disp, "value": _fmt(days), "count": cnt,
                          "breakdown": {k: _fmt(v) for k, v in by_type.items()}})

    # rank by value (ignore None)
    ranked = [it for it in items if it.get("value") is not None]
    metric = "Experience (years)" if by_experience else \
        ("Leave days taken (" + status_label + ")")
    period = ""
    if fr and to and not by_experience:
        period = fr + " to " + to

    summary = ""
    if len(ranked) >= 2:
        hi = max(ranked, key=lambda x: x["value"])
        lo = min(ranked, key=lambda x: x["value"])
        unit = "years" if by_experience else "days"
        if hi["name"] != lo["name"]:
            verb = "has the most" if by_experience else "took the most"
            verb2 = "the least"
            summary = (hi["name"] + " " + verb + " (" + str(hi["value"]) + " "
                       + unit + "), " + lo["name"] + " " + verb2 + " ("
                       + str(lo["value"]) + " " + unit + ").")

    title = ("Experience comparison" if by_experience else "Leave comparison")
    return json.dumps({
        "type": "comparison",
        "title": title,
        "metric": metric,
        "period": period,
        "unit": "years" if by_experience else "days",
        "items": items,
        "summary": summary,
    })

# ============================================================================
# ORG-WIDE / DEPARTMENT LEAVE VIEWS  (HR, Admin, or a Manager for own team)
# ----------------------------------------------------------------------------
# Three related read-only views, all built the same way: pull the relevant
# employee list, then look up each person's leaves (windowed, status-aware),
# and return a grouped "list" the frontend already renders.
#
#   * is_org_pending_query / build_org_pending
#       "show all pending leave requests across the organization"
#   * is_dept_leave_query / build_dept_leave
#       "show leave data for finance department"
#
# Scope rule (matches the rest of ENZO):
#   - is_hr or is_admin  -> whole org (or the named department)
#   - manager            -> only their own direct reports
#   - everyone else      -> politely declined
# ============================================================================

# words that name a department right before/after "department"/"dept"
_DEPT_SKIP = {"the", "of", "in", "a", "an", "for", "this", "their", "all",
              "show", "me", "leave", "leaves", "data", "pending", "requests",
              "request", "status", "across", "org", "organization",
              "organisation", "company", "whole", "entire",
              "employee", "employees", "emp", "staff", "designation",
              "balance", "balances", "id", "code", "member"}


# cache of "does this guid have direct reports" so we don't re-query the CRM
# on every message in a session.
_HAS_REPORTS_CACHE = {}


def _has_direct_reports(user, token):
    """True if at least one employee reports to this user (by manager_guid or
    manager display-name). Used when the JWT doesn't carry IsManager — some
    managers (e.g. SHASHANK) come through with IsManager missing, so we confirm
    against the CRM instead of denying them."""
    guid = str(user.get("user_guid", "")).strip().lower()
    name = clean_text(user.get("name", ""))
    cache_key = guid or name
    if not cache_key:
        return False
    if cache_key in _HAS_REPORTS_CACHE:
        return _HAS_REPORTS_CACHE[cache_key]

    found = False
    try:
        # try a direct manager_guid filter first
        q = build_dynamic_query(
            entity_name="employee",
            filters={"target": "multiple", "manager_guid": user.get("user_guid", "")},
            current_user=user)
        data = execute_crm_query(crm_query=q, token=token, user=user)
        emps = data.get("data", []) if data.get("success") else []
        # confirm client-side by guid or manager display-name
        for e in emps:
            mg = str(e.get("manager_guid", "")).strip().lower()
            mn = clean_text(e.get("manager", ""))
            if (guid and mg == guid) or (name and mn == name):
                found = True
                break
        # if the guid filter returned nothing, fall back to scanning by name
        if not found and name:
            q2 = build_dynamic_query(
                entity_name="employee",
                filters={"target": "multiple"}, current_user=user)
            d2 = execute_crm_query(crm_query=q2, token=token, user=user)
            allemps = d2.get("data", []) if d2.get("success") else []
            for e in allemps:
                if clean_text(e.get("manager", "")) == name:
                    found = True
                    break
    except Exception:
        found = False

    _HAS_REPORTS_CACHE[cache_key] = found
    return found


def _actor_scope(user, token=None):
    """Returns ('hr'|'manager'|'none'). HR/Admin see everything; a manager
    sees their own team; anyone else is denied. If the token doesn't flag the
    user as a manager, we optionally confirm via the CRM (direct reports)."""
    if user.get("is_hr") or user.get("is_admin"):
        return "hr"
    if user.get("is_manager"):
        return "manager"
    # token didn't say manager — check the CRM for direct reports
    if token is not None and _has_direct_reports(user, token):
        return "manager"
    return "none"


def _employees_in_scope(user, token, department=None, designation=None,
                        force_team=False):
    """Load the employee list this actor is allowed to see.
    HR/Admin: everyone (optionally filtered to one department/designation).
    Manager:  only direct reports (manager_guid == user's guid).

    force_team=True: scope to the actor's OWN direct reports even if they are
    HR — used for "leaves pending MY approval", which is about the people this
    person approves, not the whole org.

    Returns (employees, matched):
      matched is True only when a department/designation was requested AND at
      least one employee actually matched. When a filter is requested but
      NOTHING matches, we return ([], False) — we do NOT fall back to the whole
      org (that would silently answer with the wrong group's data)."""
    scope = _actor_scope(user, token)
    filters = {"target": "multiple"}

    team_mode = force_team or scope == "manager"

    if team_mode:
        mgr_guid = user.get("user_guid", "")
        if not mgr_guid:
            # No guid to scope by — refuse rather than risk returning the org.
            return [], False
        filters["manager_guid"] = mgr_guid
    if department:
        filters["department"] = department
    if designation:
        filters["designation"] = designation

    emp_q = build_dynamic_query(entity_name="employee",
                                filters=filters, current_user=user)
    edata = execute_crm_query(crm_query=emp_q, token=token, user=user)
    emps = edata.get("data", []) if edata.get("success") else []

    # Team scope: enforce team membership CLIENT-SIDE too. If the CRM ignored the
    # bam_manager filter (or matched by something else), we still keep only the
    # people who actually report to this manager — by guid if the record
    # carries one, otherwise by the manager's display name. This prevents a
    # manager ever seeing the whole org.
    if team_mode:
        mgr_guid = str(user.get("user_guid", "")).lower()
        mgr_name = clean_text(user.get("name", ""))
        kept = []
        for e in emps:
            e_mgr_guid = str(e.get("manager_guid", "")).lower()
            e_mgr_name = clean_text(str(e.get("manager", "")))
            if e_mgr_guid and mgr_guid and e_mgr_guid == mgr_guid:
                kept.append(e)
            elif mgr_name and e_mgr_name and mgr_name == e_mgr_name:
                kept.append(e)
        emps = kept

    if not department and not designation:
        return emps, False

    # Filter requested: keep only true matches (defensive, client-side too).
    matched = emps
    if department:
        matched = [e for e in matched
                   if department.lower() in str(e.get("department", "")).lower()]
    if designation:
        matched = [e for e in matched
                   if designation.lower() in str(e.get("designation", "")).lower()]
    return matched, bool(matched)


def _extract_department(message):
    """Pull a department name out of 'department project' / 'X department' /
    'department of X'. Prefer the word AFTER 'department' (that's the real
    name in 'employee department project'); fall back to the word before.
    Returns None if none found."""
    m = clean_text(message)
    # 1) "department project" / "dept of project" — name AFTER the keyword
    after = re.search(r"\b(?:department|dept)\s+(?:of\s+)?([a-z]+)", m)
    if after and after.group(1) not in _DEPT_SKIP:
        return after.group(1)
    # 2) "project department" — name BEFORE the keyword (skip noise words)
    before = re.search(r"\b([a-z]+)\s+(?:department|dept)\b", m)
    if before and before.group(1) not in _DEPT_SKIP:
        return before.group(1)
    return None


def _collect_leaves(emps, user, token, status_code, status_label, fr, to,
                    cap=500):
    """For each employee, fetch matching leaves and flatten into list-card
    items plus a per-department tally. Returns (items, dept_counts)."""
    items = []
    dept_counts = {}
    for e in emps[:cap]:
        guid = e.get("employee_guid")
        if not guid:
            continue
        filters = {"target": "employee", "employee_guid": guid, "top": "50"}
        if status_code:
            filters["status_code"] = status_code
            filters["status"] = status_label
        if fr and to:
            filters["from_date"] = fr
            filters["to_date"] = to
        q = build_dynamic_query(entity_name="leave_history", filters=filters,
                                current_user=user)
        data = execute_crm_query(crm_query=q, token=token, user=user)
        recs = data.get("data", []) if data.get("success") else []
        dept = e.get("department") or "—"
        for r in recs:
            frm = (r.get("from_date", "") or "")[:10]
            t = (r.get("to_date", "") or "")[:10]
            dates = frm + ((" → " + t) if t and t != frm else "")
            # status may come back as an int statuscode (e.g. 1) or a string;
            # map it to a readable label, falling back to the query's label.
            raw_status = r.get("status")
            label_for = {v: k for k, v in _STATUS.items()}  # 1 -> "requested"
            if isinstance(raw_status, (int, float)):
                badge = label_for.get(int(raw_status), status_label or "")
            elif isinstance(raw_status, str) and raw_status.strip():
                badge = raw_status
            else:
                badge = status_label or ""
            badge = str(badge).title()
            items.append({
                "primary": (e.get("employee_name") or "Employee"),
                "badge": badge,
                "fields": [["Dept", str(dept)],
                           ["Type", r.get("leave_type") or "-"],
                           ["Dates", dates],
                           ["Days", str(r.get("days") or "")]],
            })
            dept_counts[dept] = dept_counts.get(dept, 0) + 1
    return items, dept_counts


def _dept_summary_line(dept_counts):
    """'Finance 4 · Project 2 · Engineering 1' (highest first)."""
    if not dept_counts:
        return ""
    parts = sorted(dept_counts.items(), key=lambda x: x[1], reverse=True)
    return "  ·  ".join(d + " " + str(n) for d, n in parts)


# ---- ORG-WIDE PENDING -------------------------------------------------------

def _org_status_wanted(message):
    """If the message names a leave STATUS to list org-wide, return
    (status_code, label); else None. Covers pending/approved/rejected/
    cancelled."""
    m = clean_text(message)
    # "cancel request" / "cancellation request" / "pending cancellation" ->
    # the Cancel Request queue (checked before plain "cancelled").
    if re.search(r"\bcancel\s+request", m) or \
       re.search(r"\bcancellation\s+request", m) or \
       re.search(r"\bpending\s+cancel", m) or \
       re.search(r"\brequests?\s+to\s+cancel\b", m):
        return (_STATUS["cancel_request"], "cancel_request")
    if re.search(r"\b(pending|awaiting|unapproved|requested|to approve|"
                 r"for approval|need approval|approval|approvals)\b", m):
        return (_STATUS["requested"], "requested")
    if re.search(r"\b(rejected|rejection|declined|turned down|"
                 r"not approved)\b", m):
        return (_STATUS["rejected"], "rejected")
    if re.search(r"\b(approved|accepted|sanctioned|granted)\b", m):
        return (_STATUS["approved"], "approved")
    if re.search(r"\b(cancelled|canceled|cancellation)\b", m):
        return (_STATUS["cancelled"], "cancelled")
    return None


def is_org_pending_query(message):
    """Org/team-wide leave list by STATUS — 'show all pending leaves', 'show
    rejected leaves across the org', 'approved leaves in project dept', 'who
    has pending requests', 'pending approval leaves' (approver queue)."""
    m = clean_text(message)

    # A bare action verb ("approve leaves", "reject the leave", "cancel leave")
    # is an ACTION, not a list — let the action flow handle it. Only treat it
    # as a list when the user clearly wants to SEE them (show/list/pending/
    # rejected/etc. framing).
    is_bare_action = bool(re.search(r"^\s*(approve|reject|cancel|sanction|"
                                    r"grant|decline)\b", m)) and \
        not re.search(r"\b(show|list|all|display|pending|which|who|view|"
                      r"see|status)\b", m)
    if is_bare_action:
        return False

    status = _org_status_wanted(message)
    # "my team", "my department", "my dept" => the actor's TEAM (not personal).
    wants_my_team = bool(re.search(r"\bmy\s+(team|department|dept)\b", m)
                         or re.search(r"\bteam'?s\b", m)
                         or re.search(r"\bteam\s+(leaves?|members?)\b", m))
    # a group/scope word means this is a team/org view even without a status
    group_word = bool(re.search(r"\b(across|org|organization|organisation|"
                                r"company|company-wide|companywide|everyone|"
                                r"all employees|all staff|whole|entire|team|"
                                r"department|dept|sabki|sabke)\b", m))
    if not status and not group_word:
        return False
    if re.search(r"\b(my|mine|meri|mere|mera)\b", m) and not wants_my_team \
            and not group_word:
        return False
    # a cancel-request query ("cancel requests of my team") may not contain the
    # word "leave" — allow it when the cancel-request status is detected.
    _is_cancel_req = bool(status and status[1] == "cancel_request")
    if "leave" not in m and "leaves" not in m and not _is_cancel_req:
        return False
    orgwide = bool(re.search(r"\b(across|org|organization|organisation|"
                             r"company|company-wide|companywide|everyone|"
                             r"all employees|all staff|whole|entire|team|"
                             r"department|dept)\b", m))
    all_x = bool(re.search(r"\ball .*(pending|rejected|approved|cancelled)", m))
    who_x = bool(re.search(r"\bwho\s+(has|have|are|is|all)\b", m)
                 or re.search(r"\b(kaun|kon|kis|kiski|kisne|sabki|sabke)\b", m))
    approval = bool(re.search(r"\b(approval|approvals|to approve|for approval|"
                              r"need approval|awaiting approval|"
                              r"pending approval)\b", m))
    # a bare "show rejected/approved/pending leaves" is also an org list
    bare_status_list = bool(re.search(r"\b(show|list|all|display|view|see)\b", m))
    return orgwide or all_x or who_x or approval or bare_status_list


def build_org_pending(message, user, token):
    """List every REQUESTED (pending) leave in scope, with a department
    summary first, then the detail list."""
    scope = _actor_scope(user, token)
    if scope == "none":
        return ("Viewing pending leave requests across the org is available to "
                "HR or a manager (for their own team). You can still check your "
                "own — e.g. \"show my pending leaves\".")

    _day = _specific_day(message)
    if _day:
        fr, to = _day, _day
    else:
        fr, to = compute_date_range(message)  # optional window; usually none
    department = _extract_department(message)

    # "leaves pending MY approval" -> the people I approve (my team), even if
    # Scope decision:
    #  - HR/Admin: they oversee the whole org, so "pending approval" shows the
    #    ENTIRE organization (they can approve/see everyone).
    #  - A (non-HR) manager: "pending approval" = only THEIR team's requests.
    _m = clean_text(message)
    wants_org = bool(re.search(r"\b(all|across|org|organization|organisation|"
                               r"company|company-wide|companywide|everyone|"
                               r"whole|entire)\b", _m))
    # asking specifically about the APPROVAL queue (pending things to approve)
    is_approval_query = bool(re.search(r"\b(approval|approvals|to approve|"
                                       r"for approval|awaiting approval|"
                                       r"pending approval)\b", _m))
    # asking about MY TEAM's data (any status)
    wants_my_team = bool(re.search(r"\bmy\s+(team|department|dept)\b", _m)
                         or re.search(r"\bteam'?s?\b", _m)
                         or is_approval_query)
    is_hr = bool(user.get("is_hr") or user.get("is_admin"))
    # scope to the actor's team only for non-HR people (managers). HR sees org.
    force_team = wants_my_team and not wants_org and not is_hr
    # only show the "pending your approval" wording for a real approval query
    label_approval = is_approval_query and force_team

    emps, dept_matched = _employees_in_scope(user, token, department=department,
                                             force_team=force_team)

    if force_team and not emps:
        return ("You don't have any team members reporting to you, so there "
                "are no leaves pending your approval. To see the whole "
                "organization's pending leaves, ask \"show all pending leaves "
                "across the organization\".")
    if department and not dept_matched:
        return ("I couldn't find a department called \"" + department.title()
                + "\". Please check the name — e.g. \"pending leaves in Project "
                "department\".")
    if not emps:
        if department:
            return ("No employees found in the " + department.title()
                    + " department.")
        return "I couldn't load the employee list right now."

    _st = _org_status_wanted(message)
    if _st:
        _sc, _slabel = _st
    else:
        _sc, _slabel = None, ""   # no status word -> show ALL statuses
    items, dept_counts = _collect_leaves(
        emps, user, token,
        status_code=_sc, status_label=_slabel,
        fr=fr, to=to)

    # human word for the status being listed
    _stword = {"requested": "pending", "rejected": "rejected",
               "approved": "approved", "cancelled": "cancelled",
               "cancel_request": "cancellation-requested",
               "": ""}.get(_slabel, "pending")
    _sw = (_stword + " ") if _stword else ""   # spaced prefix, blank when all

    who = ("leaves pending your approval" if label_approval
           else "your team" if force_team
           else "your team" if scope == "manager"
           else (department.title() + " department") if department
           else "the organization")

    if not items:
        if label_approval:
            return ("✅ Nothing needs your approval right now — no one in your "
                    "team has a pending (requested) leave.")
        if force_team:
            return ("✅ No " + _stword + " leaves in your team right now.")
        if scope == "hr" and not department:
            return ("✅ No " + _stword + " leaves anywhere in the organization "
                    "right now.")
        return "✅ No " + _stword + " leaves in " + who + " right now."

    # Scope-clarity line — tells the user EXACTLY whose leaves these are.
    if label_approval:
        scope_note = "These are leaves awaiting your approval (your team only)."
    elif force_team:
        scope_note = ("Showing " + _sw + "leaves for your team "
                      "(the people who report to you).")
    elif scope == "hr" and not department:
        scope_note = ("Showing " + _sw + "leaves across the whole "
                      "organization (you're HR). Tip: a manager sees only "
                      "their own team.")
    elif department:
        scope_note = ("Showing " + _sw + "leaves in the "
                      + department.title() + " department.")
    else:
        scope_note = "Showing " + _sw + "leaves for your team."

    if label_approval:
        summary = ("You have " + str(len(items)) + " leave request(s) waiting "
                   "for your approval. " + scope_note)
    else:
        summary = ("There are " + str(len(items)) + " " + (_stword or "total")
                   + " leave(s). " + scope_note)
    dept_line = _dept_summary_line(dept_counts)
    if dept_line and not department:
        summary += " By department: " + dept_line + "."

    intro = ("Leaves pending your approval" if label_approval
             else (_stword.title()+" " if _stword else "") + "Leaves — " + who)
    if fr and to:
        intro += " (" + fr + " to " + to + ")"

    return json.dumps({
        "type": "list", "kind": "leave",
        "intro": intro,
        "summary": summary,
        "count": len(items), "page_size": 10, "items": items,
    })


# ---- DEPARTMENT LEAVE DATA --------------------------------------------------

def is_dept_leave_query(message):
    """'show leave data for finance department', 'leaves in project dept',
    'engineering department leave status'. Needs a department word AND a
    leave word, but NOT the on-leave phrasing (that has its own handler)."""
    m = clean_text(message)
    has_dept = bool(re.search(r"\b(department|dept)\b", m))
    has_leave = bool(re.search(r"\b(leave|leaves|leave data|leave status|"
                               r"leave requests?)\b", m))
    # let the dedicated on-leave / pending handlers win first
    if re.search(r"\b(on leave|on vacation|absent)\b", m):
        return False
    return has_dept and has_leave


def build_dept_leave(message, user, token):
    """All leaves for a named department (any status by default; honour an
    explicit status word), grouped with a per-status summary."""
    scope = _actor_scope(user, token)
    if scope == "none":
        return ("Department leave data is available to HR or a manager "
                "(for their own team).")

    department = _extract_department(message)
    if not department:
        return ("Which department? e.g. \"show leave data for finance "
                "department\".")

    m = clean_text(message)
    # default: all statuses; narrow if the user names one
    status_code, status_label = None, ""
    if re.search(r"\b(pending|requested|applied|awaiting)\b", m):
        status_code, status_label = _STATUS["requested"], "requested"
    elif re.search(r"\bapproved\b", m):
        status_code, status_label = _STATUS["approved"], "approved"
    elif re.search(r"\brejected\b", m):
        status_code, status_label = _STATUS["rejected"], "rejected"
    elif re.search(r"\bcancel", m):
        status_code, status_label = _STATUS["cancelled"], "cancelled"

    _day = _specific_day(message)
    if _day:
        fr, to = _day, _day
    else:
        fr, to = compute_date_range(message)

    emps, dept_matched = _employees_in_scope(user, token, department=department)
    if not dept_matched or not emps:
        return ("I couldn't find a department called \"" + department.title()
                + "\". Please check the name — e.g. \"leave data for Project "
                "department\".")

    items, _ = _collect_leaves(emps, user, token,
                               status_code=status_code,
                               status_label=status_label,
                               fr=fr, to=to)

    # per-status tally for the summary line
    status_counts = {}
    for it in items:
        b = (it.get("badge") or "—")
        status_counts[b] = status_counts.get(b, 0) + 1

    dept_title = department.title()
    if not items:
        extra = (" " + status_label if status_label else "")
        return "✅ No" + extra + " leaves found for the " + dept_title + " department."

    status_line = "  ·  ".join(k + " " + str(v)
                               for k, v in sorted(status_counts.items(),
                                                  key=lambda x: x[1], reverse=True))
    summary = (dept_title + " department has " + str(len(items))
               + " leave record(s)." + (" By status: " + status_line + "."
                                        if status_line else ""))

    intro = dept_title + " department — leave data"
    if status_label:
        intro += " (" + status_label + ")"
    if fr and to:
        intro += " · " + fr + " to " + to

    return json.dumps({
        "type": "list", "kind": "leave",
        "intro": intro,
        "summary": summary,
        "count": len(items), "page_size": 10, "items": items,
    })


# ============================================================================
# GROUP LEAVE BALANCE  — balance for everyone in a department or designation
# ----------------------------------------------------------------------------
#   "leave balance for project department"
#   "show leave balance for designation Team Member"
# Returns a 'balance_group' (one group per employee) the frontend renders.
# Scope: HR/Admin (whole org / named group), Manager (own team).
# ============================================================================

def _extract_designation(message):
    """Pull a designation out of 'designation X' / 'X designation'. Handles
    two-word titles like 'team member', 'software developer'. None if absent."""
    m = clean_text(message)
    # designation skip set — like _DEPT_SKIP but KEEP role words like "member"
    dskip = _DEPT_SKIP - {"member"}
    # "designation team member" — take the phrase after the keyword
    after = re.search(r"\bdesignation\s+(?:of\s+)?([a-z]+(?:\s+[a-z]+)?)", m)
    if after:
        cand = " ".join(w for w in after.group(1).split() if w not in dskip)
        if cand:
            return cand
    # "team member designation"
    before = re.search(r"\b([a-z]+(?:\s+[a-z]+)?)\s+designation\b", m)
    if before:
        cand = " ".join(w for w in before.group(1).split() if w not in dskip)
        if cand:
            return cand
    return None


def is_group_balance_query(message):
    """True for 'leave balance of <department|designation|team|everyone> ...' —
    a balance read scoped to a GROUP of people (not one named person)."""
    m = clean_text(message)
    if not re.search(r"\bbalance\b", m):
        return False
    # a specific named person's balance is NOT a group query
    # (handled by the normal balance flow)
    has_dept = bool(re.search(r"\b(department|dept)\b", m))
    has_desig = bool(re.search(r"\bdesignation\b", m))
    # group words: team, all employees/members/staff, everyone
    has_group = bool(re.search(r"\b(team|team members?|all (employees?|members?|"
                               r"staff)|everyone|sabki|sabke|whole team|"
                               r"my team|entire team)\b", m))
    return has_dept or has_desig or has_group


def build_group_balance(message, user, token):
    """Per-employee leave balance for every person in a department or
    designation, returned as a 'balance_group'."""
    scope = _actor_scope(user, token)
    if scope == "none":
        return ("Group leave balances are available to HR or a manager "
                "(for their own team). For one person try "
                "\"show leave balance for <name or employee id>\".")

    department = _extract_department(message)
    designation = _extract_designation(message)

    _m = clean_text(message)
    # "team members / my team / all employees / everyone" — no dept/designation,
    # scope to the actor's TEAM (manager) or the whole ORG (HR).
    wants_team = bool(re.search(r"\b(team|team members?|all (employees?|members?|"
                                r"staff)|everyone|sabki|sabke|whole team|"
                                r"my team|entire team)\b", _m))
    is_hr = bool(user.get("is_hr") or user.get("is_admin"))

    if not department and not designation and not wants_team:
        return ("Which group? e.g. \"leave balance for Project department\", "
                "\"leave balance for designation Team Member\", or "
                "\"leave balance of my team\".")

    if department or designation:
        emps, matched = _employees_in_scope(user, token,
                                            department=department,
                                            designation=designation)
        if not matched or not emps:
            label = (department.title() + " department") if department \
                else ("designation " + designation.title())
            return ("I couldn't find anyone under " + label + ". "
                    "Please check the name.")
    else:
        # team/all scope: managers -> own team; HR -> whole org
        emps, _ = _employees_in_scope(user, token, force_team=not is_hr)
        if not emps:
            if not is_hr:
                return ("You don't have any team members reporting to you, so "
                        "there are no team balances to show.")
            return "I couldn't load the employee list right now."

    # one balance lookup per employee -> grouped cards
    groups = []
    for e in emps[:500]:
        guid = e.get("employee_guid")
        nm = e.get("employee_name") or "Employee"
        nm = nm.title() if isinstance(nm, str) and nm.isupper() else nm
        if not guid:
            continue
        if not can_read_entity(entity="leave", current_user=user,
                               target_employee=guid, token=token):
            groups.append({"name": nm, "denied": True, "items": []})
            continue
        q = build_dynamic_query(
            entity_name="leave",
            filters={"target": "employee", "employee_guid": guid},
            current_user=user)
        data = execute_crm_query(crm_query=q, token=token, user=user)
        recs = data.get("data", []) if data.get("success") else []
        items = []
        for r in recs:
            try:
                bal = float(r.get("balance"))
            except (TypeError, ValueError):
                bal = None
            items.append({"type": r.get("leave_type") or "Leave",
                          "balance": bal})
        groups.append({"name": nm, "items": items})

    if not groups:
        return "No employees found for that group."

    who = ((department.title() + " department") if department
           else ("designation " + designation.title()) if designation
           else "your team" if not is_hr
           else "the organization")
    return json.dumps({
        "type": "balance_group",
        "intro": "Leave balance — " + who + " (" + str(len(groups)) + " people)",
        "groups": groups,
    })


# ============================================================================
# LOW LEAVE BALANCE  — who has less than N days of balance left
# ----------------------------------------------------------------------------
#   "who in my team has less than 2 days of leave balance left?"
#   "team members with low leave balance"
#   "show employees with balance below 5"          (HR -> org)
# Scope: Manager -> own team; HR/Admin -> whole org (or a named department).
# Returns a 'balance_group' listing only the people under the threshold, with
# each person's low leave-type balance(s) shown.
# ============================================================================

_DEFAULT_LOW_THRESHOLD = 2.0   # "low" with no number given


def _low_threshold(message):
    """Parse the balance cutoff. 'less than 2', 'below 3', 'under 5',
    'fewer than 1', '<= 2' -> that number. Bare 'low balance' -> default."""
    m = clean_text(message)
    num = re.search(r"\b(?:less than|lower than|fewer than|below|under|"
                    r"lesser than|<=?|at most|max)\s*(\d+(?:\.\d+)?)", m)
    if num:
        try:
            return float(num.group(1))
        except ValueError:
            pass
    # "2 days or less", "only N left"
    num2 = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:days?)?\s*(?:or (?:less|fewer)|left)\b", m)
    if num2:
        try:
            return float(num2.group(1))
        except ValueError:
            pass
    return _DEFAULT_LOW_THRESHOLD


def is_low_balance_query(message):
    """True for 'who has less than N days of balance', 'team members with low
    leave balance', 'employees running low on leave'."""
    m = clean_text(message)
    if not re.search(r"\b(balance|leaves?|days?)\b", m):
        return False
    has_low = bool(re.search(r"\b(less than|lower than|fewer than|below|under|"
                             r"lesser than|running low|low(?:est)?|nearly out|"
                             r"almost out|at most|<=?)\b", m))
    # needs a "who / which / team / employees / staff" people-scope cue, so we
    # don't collide with the user's own "is my balance low" (that's self).
    people = bool(re.search(r"\b(who|which|team|teammates?|members?|employees?|"
                            r"staff|people|everyone|anyone|reportees?|reports)\b", m))
    # explicit self -> not this handler
    if re.search(r"\b(my own|do i|am i|is my|meri|mera)\b", m):
        return False
    return has_low and people


def build_low_balance(message, user, token):
    """List people (in the actor's scope) whose remaining balance is under the
    threshold, as a 'balance_group' showing only their low leave types."""
    scope = _actor_scope(user, token)
    if scope == "none":
        return ("Checking who's low on leave balance is available to a manager "
                "(for their team) or HR. You can still ask \"what's my leave "
                "balance?\".")

    threshold = _low_threshold(message)
    department = _extract_department(message)  # HR may scope to a department

    # Managers are always limited to their own team (department ignored for
    # them — their team is the scope). HR may narrow to a department.
    dep_for_scope = department if scope == "hr" else None
    emps, matched = _employees_in_scope(user, token, department=dep_for_scope)

    if dep_for_scope and not matched:
        return ("I couldn't find a department called \"" + department.title()
                + "\". Please check the name.")
    if not emps:
        if scope == "manager":
            return "You don't have any team members reporting to you."
        return "I couldn't load the employee list right now."

    groups = []
    for e in emps[:500]:
        guid = e.get("employee_guid")
        nm = e.get("employee_name") or "Employee"
        nm = nm.title() if isinstance(nm, str) and nm.isupper() else nm
        if not guid:
            continue
        if not can_read_entity(entity="leave", current_user=user,
                               target_employee=guid, token=token):
            continue
        q = build_dynamic_query(
            entity_name="leave",
            filters={"target": "employee", "employee_guid": guid},
            current_user=user)
        data = execute_crm_query(crm_query=q, token=token, user=user)
        recs = data.get("data", []) if data.get("success") else []

        low_items = []
        for r in recs:
            try:
                bal = float(r.get("balance"))
            except (TypeError, ValueError):
                continue
            if bal < threshold:
                low_items.append({"type": r.get("leave_type") or "Leave",
                                  "balance": bal})
        if low_items:
            groups.append({"name": nm, "items": low_items})

    who = ("your team" if scope == "manager"
           else (department.title() + " department") if department
           else "the organization")
    thr = int(threshold) if float(threshold).is_integer() else threshold

    if not groups:
        return ("✅ Nobody in " + who + " is below " + str(thr)
                + " day(s) of leave balance.")

    return json.dumps({
        "type": "balance_group",
        "intro": ("Low leave balance in " + who + " (under " + str(thr)
                  + " day(s)) — " + str(len(groups)) + " "
                  + ("person" if len(groups) == 1 else "people")),
        "groups": groups,
    })


# ============================================================================
# LEAVE REASON / "why was my leave rejected"  — reason lookup for one date
# ----------------------------------------------------------------------------
#   "Why was my leave on 13 July rejected?"
#   "What was the reason for my leave on 20 july?"
# Logic:
#   * find the person's leave(s) whose range covers that date
#   * if a REJECTED one exists  -> show bam_rejectionreason (or "no reason given")
#   * else (requested/approved) -> show the applicant's own bam_leavereason
#   * if no leave on that date   -> say so politely
# Defaults to the current user; HR/manager may name a person (auth-gated).
# ============================================================================

def is_leave_reason_query(message):
    """True for 'why was my leave rejected on <date>', 'reason for my leave on
    <date>'. Needs a reason/why QUESTION + a leave word + a specific day.
    Must NOT fire on an apply request that merely contains the word 'reason'
    (e.g. 'apply sick leave ... reason fever')."""
    m = clean_text(message)
    # An apply/cancel/approve action is NOT a reason lookup — bail out.
    if re.search(r"\b(apply|applying|applied|cancel|cancelling|approve|"
                 r"approving|reject|rejecting|submit|take|book)\b", m):
        # exception: "why was it rejected" uses 'rejected' as a question, not
        # an action — allow only when a clear question cue is present.
        if not re.search(r"\b(why|reason for|what.*reason|kyun|kyu)\b", m):
            return False
    day = _specific_day(message)
    if not day:
        return False
    has_leave = "leave" in m or "leaves" in m
    if not has_leave:
        return False
    # question framing: "why ...", "reason for ...", "what was the reason",
    # "rejected?/declined?" — but NOT "reason <freetext>" (value-giving form).
    is_why = bool(re.search(r"\bwhy\b", m)) or bool(re.search(r"\bkyun?\b", m))
    is_reason_for = bool(re.search(r"\breason\s+(for|behind|of)\b", m))
    is_status_q = bool(re.search(r"\b(rejected|rejection|declined|"
                                 r"not approved|turned down)\b", m))
    return is_why or is_reason_for or is_status_q


def _covers(rec, day):
    """Does a leave record's date range include `day` (YYYY-MM-DD)?"""
    frm = (rec.get("from_date", "") or "")[:10]
    to = (rec.get("to_date", "") or "")[:10] or frm
    if not frm:
        return False
    return frm <= day <= to


def build_leave_reason(message, user, token):
    day = _specific_day(message)
    if not day:
        return ("Which date? e.g. \"why was my leave on 13 July rejected?\"")

    # Resolve target: self by default; a named person only for HR/manager.
    target_guid = user.get("user_guid", "")
    target_name = "your"
    names = extract_comparison_names(message)  # reuses name cleaner
    # only treat as "other person" if HR/admin/manager AND a real name found
    is_privileged = bool(user.get("is_hr") or user.get("is_admin")
                         or user.get("is_manager"))
    if is_privileged and names:
        res = resolve_employee(employee_name=names[0], token=token, user=user)
        recs = res.get("data", []) if res.get("success") else []
        if recs:
            g = recs[0].get("employee_guid")
            if not can_read_entity(entity="leave_history", current_user=user,
                                   target_employee=g, token=token):
                return "You are not authorized to view that employee's leave."
            target_guid = g
            nm = recs[0].get("employee_name") or names[0]
            target_name = (nm.title() if nm.isupper() else nm) + "'s"

    if not target_guid:
        return "I couldn't identify whose leave to look up."

    # Pull recent leaves for the person and find the one covering that day.
    q = build_dynamic_query(
        entity_name="leave_history",
        filters={"target": "employee", "employee_guid": target_guid,
                 "top": "100"},
        current_user=user)
    data = execute_crm_query(crm_query=q, token=token, user=user)
    recs = data.get("data", []) if data.get("success") else []

    matches = [r for r in recs if _covers(r, day)]
    if not matches:
        return ("I couldn't find " + target_name + " leave on " + day
                + ". There's no leave record for that date.")

    label_for = {v: k for k, v in _STATUS.items()}

    def _status_of(r):
        s = r.get("status")
        if isinstance(s, (int, float)):
            return label_for.get(int(s), "")
        return str(s or "").lower()

    # What is the user actually asking about?
    #   "why was it rejected" / "rejection reason"  -> the REJECTION reason
    #   "leave reason" / "what reason did I give"    -> the APPLICANT's reason
    _m = clean_text(message)
    asks_rejection = bool(re.search(r"\b(reject|rejected|rejection|declined|"
                                    r"decline|turned down|not approved|"
                                    r"disapprov)\b", _m))

    def _pick(status_wanted):
        for r in matches:
            if _status_of(r) == status_wanted:
                return r
        return None

    if asks_rejection:
        # user explicitly wants the rejection story -> prefer a rejected record
        rec = _pick("rejected") or matches[0]
    else:
        # plain "leave reason" -> prefer the applicant's own applied/approved
        # record; only fall back to a rejected one if nothing else exists.
        rec = (_pick("requested") or _pick("approved") or _pick("cancelled")
               or matches[0])

    status = _status_of(rec)
    lt = rec.get("leave_type") or "leave"
    frm = (rec.get("from_date", "") or "")[:10]
    to = (rec.get("to_date", "") or "")[:10] or frm
    span = frm if frm == to else (frm + " to " + to)

    poss = target_name  # "your" / "Purav's"
    subj = "Your" if poss == "your" else poss

    # ---- REJECTION path: only when the user asked about rejection ----
    if asks_rejection and status == "rejected":
        rr = (rec.get("rejection_reason") or "").strip()
        if rr:
            return (subj + " " + lt + " (" + span + ") was rejected.\n"
                    "Rejection reason: " + rr)
        return (subj + " " + lt + " (" + span + ") was rejected, but no "
                "rejection reason was recorded. Please check with the "
                "approving manager for details.")

    # If they asked about rejection but this record isn't rejected, say so.
    if asks_rejection and status != "rejected":
        return (subj + " " + lt + " (" + span + ") is " + (status or "on record")
                + ", not rejected. So there's no rejection reason for it.")

    # ---- APPLICANT REASON path: plain "leave reason" ----
    lr = (rec.get("leave_reason") or "").strip()
    status_word = status or "on record"
    if lr:
        return (subj + " " + lt + " (" + span + ") is " + status_word + ".\n"
                "Reason you gave: " + lr)
    return (subj + " " + lt + " (" + span + ") is " + status_word + ", but no "
            "reason was recorded for it.")