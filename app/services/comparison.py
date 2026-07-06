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


def build_on_leave(message, user, token):
    """Scan the org for approved leaves overlapping a date window (default:
    today) and list who is on leave. HR/admin only."""
    import datetime
    if not (user.get("is_hr") or user.get("is_admin")):
        return ("Viewing who's on leave across the org is available to HR only. "
                "You can still check one person — e.g. \"show Purav's leaves next week\".")

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