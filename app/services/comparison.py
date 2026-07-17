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
           "cancelled": 100010003, "rejected": 100010004}


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


def _actor_scope(user):
    """Returns ('hr'|'manager'|'none'). HR/Admin see everything; a manager
    sees their own team; anyone else is denied."""
    if user.get("is_hr") or user.get("is_admin"):
        return "hr"
    if user.get("is_manager"):
        return "manager"
    return "none"


def _employees_in_scope(user, token, department=None, designation=None):
    """Load the employee list this actor is allowed to see.
    HR/Admin: everyone (optionally filtered to one department/designation).
    Manager:  only direct reports (manager_guid == user's guid).

    Returns (employees, matched):
      matched is True only when a department/designation was requested AND at
      least one employee actually matched. When a filter is requested but
      NOTHING matches, we return ([], False) — we do NOT fall back to the whole
      org (that would silently answer with the wrong group's data)."""
    scope = _actor_scope(user)
    filters = {"target": "multiple"}

    if scope == "manager":
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

    # Manager: enforce team membership CLIENT-SIDE too. If the CRM ignored the
    # bam_manager filter (or matched by something else), we still keep only the
    # people who actually report to this manager — by guid if the record
    # carries one, otherwise by the manager's display name. This prevents a
    # manager ever seeing the whole org.
    if scope == "manager":
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

def is_org_pending_query(message):
    """'show all pending leave requests across the organization',
    'pending leaves company-wide', 'who has pending leave requests',
    'pending leaves in finance department'."""
    m = clean_text(message)
    pending = bool(re.search(r"\b(pending|awaiting|unapproved|requested|"
                             r"to approve|for approval|need approval)\b", m))
    if not pending:
        return False
    # self-scoped ("my/mine") pending is handled elsewhere — not org-wide
    if re.search(r"\b(my|mine|meri|mere|mera)\b", m):
        return False
    leave = "leave" in m or "leaves" in m
    if not leave:
        return False
    orgwide = bool(re.search(r"\b(across|org|organization|organisation|"
                             r"company|company-wide|companywide|everyone|"
                             r"all employees|all staff|whole|entire|team|"
                             r"department|dept)\b", m))
    all_pending = bool(re.search(r"\ball .*pending", m))
    # "who has/have pending ..." — plainly asking about other people
    who_pending = bool(re.search(r"\bwho\s+(has|have|are|is)\b", m))
    return orgwide or all_pending or who_pending


def build_org_pending(message, user, token):
    """List every REQUESTED (pending) leave in scope, with a department
    summary first, then the detail list."""
    scope = _actor_scope(user)
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

    emps, dept_matched = _employees_in_scope(user, token, department=department)
    if department and not dept_matched:
        return ("I couldn't find a department called \"" + department.title()
                + "\". Please check the name — e.g. \"pending leaves in Project "
                "department\".")
    if not emps:
        if department:
            return ("No employees found in the " + department.title()
                    + " department.")
        return "I couldn't load the employee list right now."

    items, dept_counts = _collect_leaves(
        emps, user, token,
        status_code=_STATUS["requested"], status_label="requested",
        fr=fr, to=to)

    who = ("your team" if scope == "manager"
           else (department.title() + " department") if department
           else "the organization")

    if not items:
        return "✅ No pending leave requests in " + who + " right now."

    summary = ("There are " + str(len(items)) + " pending leave request(s) in "
               + who + ".")
    dept_line = _dept_summary_line(dept_counts)
    if dept_line and not department:
        summary += " By department: " + dept_line + "."

    intro = "Pending leave requests — " + who
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
    scope = _actor_scope(user)
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
    """True for 'leave balance for <department|designation> ...' — a balance
    read scoped to a whole department or designation (not one person)."""
    m = clean_text(message)
    if not re.search(r"\bbalance\b", m):
        return False
    # must name a department or a designation
    has_dept = bool(re.search(r"\b(department|dept)\b", m))
    has_desig = bool(re.search(r"\bdesignation\b", m))
    return has_dept or has_desig


def build_group_balance(message, user, token):
    """Per-employee leave balance for every person in a department or
    designation, returned as a 'balance_group'."""
    scope = _actor_scope(user)
    if scope == "none":
        return ("Group leave balances are available to HR or a manager "
                "(for their own team). For one person try "
                "\"show leave balance for <name or employee id>\".")

    department = _extract_department(message)
    designation = _extract_designation(message)

    if not department and not designation:
        return ("Which group? e.g. \"leave balance for Project department\" or "
                "\"leave balance for designation Team Member\".")

    emps, matched = _employees_in_scope(user, token,
                                        department=department,
                                        designation=designation)
    if not matched or not emps:
        label = (department.title() + " department") if department \
            else ("designation " + designation.title())
        return ("I couldn't find anyone under " + label + ". "
                "Please check the name.")

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

    who = (department.title() + " department") if department \
        else ("designation " + designation.title())
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
    scope = _actor_scope(user)
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