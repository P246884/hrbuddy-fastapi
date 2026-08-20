"""
Comp-Off (compensatory off) requests — a SEPARATE workflow from regular leave.

Rules (per the .NET portal):
  * Employee applies a comp-off request (project, dates, remark).
  * ONLY the employee's MANAGER can approve/reject it (never HR directly —
    HR can only VIEW comp-off requests, same as everyone can view their own).
  * On approval, an expiry window (90 days from the request's start date,
    crc08_expirydate) is set — an expired comp-off cannot be approved, and
    once expired it cannot be availed either.
  * On approval, the employee's balance gains a new "Comp Off" leave-type
    entry they can then use like any other leave.

This module is purely ADDITIVE — it does not touch the existing leave
apply/approve/reject/balance flows.
"""

import json
import re
import datetime

from app.intent.fast_intent import clean_text, compute_date_range
from app.crm.crm_query_builder import build_dynamic_query
from app.crm.crm_executor import execute_crm_query
from app.services.leave_action_executor import (
    resolve_employee, extract_reason_from_message, _reason_picker_response,
    _error_response, _success_response, call_hrbuddy_api,
)

# CONFIRMED status codes (live CRM optionset on bam_compoff.statuscode):
#   Active=1, Approved=810100000, Requested=810100001, Cancelled=810100002,
#   Draft=810100003, Availed=810100004, Applied For Leave=810100005,
#   Reject=810100006, Expired=810100007, Inactive=2
_STATUS = {"requested": 810100001, "approved": 810100000,
           "rejected": 810100006, "cancelled": 810100002,
           "draft": 810100003, "availed": 810100004,
           "applied_for_leave": 810100005, "expired": 810100007}
_LABEL = {810100000: "Approved", 810100001: "Requested",
          810100002: "Cancelled", 810100003: "Draft",
          810100004: "Availed", 810100005: "Applied For Leave",
          810100006: "Rejected", 810100007: "Expired"}
# Draft (810100003) is a separate pre-submission state, NOT the same as
# Requested — a manager only sees/actions requests that reached "Requested".
_REQUESTED_CODES = {810100001}


def _calendar_days(fr, to):
    try:
        d1 = datetime.date.fromisoformat(fr[:10])
        d2 = datetime.date.fromisoformat(to[:10])
        return max(1, (d2 - d1).days + 1)
    except (ValueError, TypeError):
        return 1


def _is_any_manager(user, token):
    if user.get("is_manager"):
        return True
    try:
        from app.services.comparison import _has_direct_reports
        return _has_direct_reports(user, token)
    except Exception:
        return False


# ============================================================================
# DETECTION
# ============================================================================

def is_compoff_query(message):
    """True for anything mentioning comp-off (apply/show/approve/reject/list)."""
    m = clean_text(message)
    return bool(re.search(r"\bcomp[\s-]?off\b", m))


def is_apply_compoff_query(message):
    m = clean_text(message)
    if not is_compoff_query(m):
        return False
    return bool(re.search(r"\b(apply|request|raise|create|want|need|file|"
                          r"avail)\b", m)) and not re.search(
                          r"\b(show|list|view|my requests?|history|status)\b", m)


def is_view_compoff_query(message):
    """'show my comp off requests', 'show comp off requests of my team',
    'show all comp off requests' — a LIST, not an action."""
    m = clean_text(message)
    if not is_compoff_query(m):
        return False
    if is_apply_compoff_query(m):
        return False
    if re.search(r"\b(approve|reject)\b", m):
        return False
    return True


def detect_compoff_action(message):
    """Returns 'approve_compoff' / 'reject_compoff' / None."""
    m = clean_text(message)
    if not is_compoff_query(m):
        return None
    if re.search(r"^\s*approve\b", m) or re.search(r"\bapprove\b.*comp[\s-]?off", m):
        return "approve_compoff"
    if re.search(r"^\s*reject\b", m) or re.search(r"\breject\b.*comp[\s-]?off", m):
        return "reject_compoff"
    return None


# ============================================================================
# VIEW
# ============================================================================

def _status_wanted(message):
    m = clean_text(message)
    if re.search(r"\b(pending|requested|awaiting|to approve|for approval)\b", m):
        return _STATUS["requested"]
    if re.search(r"\b(approved|accepted)\b", m):
        return _STATUS["approved"]
    if re.search(r"\b(rejected|declined)\b", m):
        return _STATUS["rejected"]
    if re.search(r"\b(cancelled|canceled)\b", m):
        return _STATUS["cancelled"]
    if re.search(r"\bexpired\b", m):
        return _STATUS["expired"]
    if re.search(r"\bavailed\b", m):
        return _STATUS["availed"]
    return None


def _label_for(code):
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code or "")
    return _LABEL.get(code, str(code))


def _fetch_compoff(employee_guid, user, token, status_code=None, top="50"):
    filters = {"target": "employee", "employee_guid": employee_guid, "top": top}
    if status_code:
        filters["status_code"] = status_code
        # the query builder maps filters["status"] (a LABEL) -> statuscode via
        # its global STATUS_MAPPING; compoff codes aren't in that map, so we
        # add them locally (comparison._STATUS-style) and pass a matching label
        # the builder can resolve, falling back to a raw statuscode filter.
        _rev = {v: k for k, v in _STATUS.items()}
        label = _rev.get(status_code)
        if label:
            filters["status"] = label
    q = build_dynamic_query(entity_name="compoff", filters=filters, current_user=user)
    data = execute_crm_query(crm_query=q, token=token, user=user)
    return data.get("data", []) if data.get("success") else []


def build_view_compoff(message, user, token):
    m = clean_text(message)
    status_code = _status_wanted(message)

    wants_team = bool(re.search(r"\b(my team|team members?|of my team)\b", m))
    wants_org = bool(re.search(r"\b(all|across|org|organization|organisation|"
                               r"everyone|company)\b", m))
    is_hr = bool(user.get("is_hr") or user.get("is_admin"))

    if wants_team or (wants_org and not is_hr):
        from app.services.comparison import _employees_in_scope
        emps, _ = _employees_in_scope(user, token, force_team=not is_hr)
        who = "your team"
    elif wants_org and is_hr:
        from app.services.comparison import _employees_in_scope
        emps, _ = _employees_in_scope(user, token)
        who = "the organization"
    else:
        emps = [{"employee_guid": user.get("user_guid"),
                 "employee_name": user.get("name", "You")}]
        who = "you"

    if not emps:
        return "No employees found for that scope."

    items = []
    for e in emps[:200]:
        g = e.get("employee_guid")
        nm = e.get("employee_name") or "Employee"
        if not g:
            continue
        recs = _fetch_compoff(g, user, token, status_code=status_code)
        for r in recs:
            fd = str(r.get("from_date", ""))[:10]
            td = str(r.get("to_date", ""))[:10]
            span = fd if fd == td else (fd + " \u2192 " + td)
            badge = _label_for(r.get("status"))
            items.append({
                "primary": nm,
                "badge": badge,
                "fields": [["Project", r.get("project") or "-"],
                           ["Dates", span],
                           ["Requested days", str(r.get("requested_days") or "")],
                           ["Approved days", str(r.get("approved_days") or "-")],
                           ["Reason", r.get("request_reason") or "-"]],
            })

    if not items:
        return "✅ No comp-off requests found for " + who + "."

    intro = "Comp-off requests \u2014 " + who + " (" + str(len(items)) + ")"
    return json.dumps({
        "type": "list", "kind": "compoff",
        "intro": intro,
        "count": len(items), "page_size": 10, "items": items,
    })


# ============================================================================
# APPLY
# ============================================================================

def _my_assigned_projects(user, token):
    """Return [(id, name), ...] of projects the .NET portal shows in the
    'Select project' dropdown for this employee. Asks .NET directly (same
    method the working portal page uses) instead of guessing CRM field
    names on the Python side."""
    try:
        response = call_hrbuddy_api(
            endpoint="/api/hrbuddy/execute-action",
            token=token, user=user, method="POST",
            body={"action": "get_my_projects",
                  "payload": {"employee_guid": user.get("user_guid")}})
        if not response.get("success"):
            return []
        rows = response.get("projects") or []
    except Exception:
        rows = []
    seen, out = set(), []
    for r in rows:
        pid = r.get("id")
        nm = r.get("name")
        if pid and nm and pid not in seen:
            seen.add(pid)
            out.append((pid, nm))
    return out


def _resolve_project(name_hint, user, token):
    """Match a typed project name against THIS employee's own assigned
    projects (the same list the picker shows) — not the whole org's list."""
    if not name_hint:
        return None, None
    projects = _my_assigned_projects(user, token)
    hint = name_hint.strip().lower()
    matches = [(pid, nm) for pid, nm in projects if hint in nm.lower()]
    if len(matches) == 1:
        return matches[0]
    return None, None


def _employee_manager_name(user, token):
    """Cosmetic only — fetch the employee's manager name to mention in the
    chat reply. Never affects authorization/blocking."""
    try:
        response = call_hrbuddy_api(
            endpoint="/api/hrbuddy/execute-action",
            token=token, user=user, method="POST",
            body={"action": "get_employee_manager",
                  "payload": {"employee_guid": user.get("user_guid")}})
        if response.get("success"):
            return response.get("managerName")
    except Exception:
        pass
    return None


def _extract_project_hint(message):
    m = clean_text(message)
    pm = re.search(r"\bproject\s+([a-z0-9 &.'-]+?)(?:\s+reason\b|\s+from\b|"
                  r"\s+on\b|\s+for\b|$)", m)
    return pm.group(1).strip() if pm else ""


def build_apply_compoff(message, user, token, pending_context=None):
    """Comp-off apply needs a project, dates, total days and a remark.
    Slot order: PROJECT first (picker of assigned projects, or a typed name),
    then dates, then reason — matching the .NET portal's form order."""
    ctx = pending_context or {}

    # ---- 1) PROJECT ----
    project_guid = ctx.get("project_guid")
    project_name = ctx.get("project_name")
    if not project_guid:
        hint = _extract_project_hint(message)
        if hint:
            project_guid, project_name = _resolve_project(hint, user, token)
            if not project_guid:
                return _error_response(
                    "I couldn't match \"" + hint + "\" to exactly one "
                    "project. Please check the spelling, or just say "
                    "\"apply comp off\" and I'll show your assigned "
                    "projects to pick from."), None
        else:
            # nothing typed — offer a PICKER of the employee's assigned
            # projects (mirrors the portal's "Select project" dropdown).
            my_projects = _my_assigned_projects(user, token)
            if my_projects:
                names = [nm for g, nm in my_projects]
                return json.dumps({
                    "type": "type_picker",
                    "message": "Which project is this comp-off for?",
                    "options": names,
                    "context": {"_awaiting_project": True},
                }), None
            # no assigned-projects data available — fall back to asking by name
            return _error_response(
                "Which project is this comp-off for? e.g. \"apply comp off "
                "project Apar Website\"."), None

    _message_is_dates_only = bool(ctx.get("_message_is_dates_only"))

    # ---- 2) DATES ----
    fr = ctx.get("from_date")
    to = ctx.get("to_date")
    if not fr:
        from app.services.comparison import _specific_day
        d = _specific_day(message)
        if d:
            fr = to = d
        else:
            fr, to = compute_date_range(message)

    if not fr or not to:
        mgr = _employee_manager_name(user, token)
        mgr_line = (" It'll go to " + mgr + " for approval.") if mgr else ""
        return json.dumps({
            "type": "date_picker",
            "hide_half_day": True,
            "message": "Got it — " + (project_name or "that project") + "." +
                      mgr_line + " Select the comp-off date(s):",
            "context": {"action": "apply_compoff",
                       "project_guid": project_guid, "project_name": project_name},
        }), None

    # ---- 3) REASON ----
    reason = ctx.get("reason")
    if not reason and not _message_is_dates_only:
        reason = extract_reason_from_message(message)
    if not reason:
        return _reason_picker_response(
            "Please add a reason/remark for this comp-off request:",
            {"_reason_submit": True, "action": "apply_compoff",
             "from_date": fr, "to_date": to,
             "project_guid": project_guid, "project_name": project_name}), None

    total_days = str(_calendar_days(fr, to))

    payload = {
        "employee_guid": user.get("user_guid"),
        "project_guid": project_guid,
        "from_date": fr, "to_date": to,
        "total_days": total_days,
        "reason": reason,
    }
    response = call_hrbuddy_api(
        endpoint="/api/hrbuddy/execute-action",
        token=token, user=user, method="POST",
        body={"action": "apply_compoff", "payload": payload})
    if not response.get("success"):
        return _error_response(response.get("message",
                               "Failed to submit comp-off request.")), None
    return _success_response(
        "\u2705 Comp-off request submitted for " + fr +
        (" to " + to if to != fr else "") +
        (" (" + project_name + ")" if project_name else "") +
        ". Your manager will review it."), None


# ============================================================================
# APPROVE / REJECT  (manager only — never HR directly)
# ============================================================================

def _is_expired(record):
    # CRM has a dedicated "Expired" statuscode (810100007) — prefer that.
    s = record.get("status")
    try:
        if int(s) == _STATUS["expired"]:
            return True
    except (TypeError, ValueError):
        pass
    # fall back to the expiry date, in case the record hasn't been
    # transitioned to "Expired" yet but the date has already passed.
    exp = record.get("expiry_date") or ""
    exp = str(exp)[:10]
    if not exp:
        return False
    try:
        exp_date = datetime.date.fromisoformat(exp)
        return exp_date < datetime.date.today()
    except ValueError:
        return False


def _team_compoff_picker(user, token, action, status_codes):
    from app.services.comparison import _employees_in_scope
    emps, _ = _employees_in_scope(user, token, force_team=True)
    if not emps:
        return None
    if not isinstance(status_codes, (list, tuple)):
        status_codes = [status_codes]

    options = []
    for e in emps:
        g = e.get("employee_guid")
        nm = e.get("employee_name", "")
        if not g:
            continue
        for sc in status_codes:
            recs = _fetch_compoff(g, user, token, status_code=sc)
            for r in recs:
                fd = str(r.get("from_date", ""))[:10]
                td = str(r.get("to_date", ""))[:10]
                span = fd if fd == td else (fd + " \u2192 " + td)
                expired_tag = " | \u26a0\ufe0f EXPIRED" if _is_expired(r) else ""
                status_tag = (" | \u21bb was Rejected" if sc == _STATUS["rejected"]
                             else "")
                label = (nm + " \u2014 " + span + " (" +
                         str(r.get("requested_days") or "") + " days)" +
                         status_tag + expired_tag)
                options.append({"label": label,
                                "compoff_guid": r.get("compoff_guid", ""),
                                "expired": _is_expired(r)})
    if not options:
        return None

    verb = "approve" if action == "approve_compoff" else "reject"
    return json.dumps({
        "type": "leave_picker",   # reuse the same frontend picker component
        "message": "Select a comp-off request to " + verb + " (your team):",
        "leaves": options,
        "action": action,
        "page_size": 4,
        "context": {},
    })


def build_approve_reject_compoff(message, user, token, action, pending_context=None):
    """action: 'approve_compoff' | 'reject_compoff'."""
    is_hr_admin = bool(user.get("is_hr") or user.get("is_admin"))
    # HR can VIEW comp-off but NEVER approve/reject directly — only a manager
    # (someone with direct reports) can action a comp-off request.
    if not _is_any_manager(user, token):
        if is_hr_admin:
            return _error_response(
                "Comp-off requests can only be approved by the employee's "
                "manager — HR can view them but not action them directly."), None
        return _error_response(
            "You are not authorized to " +
            ("approve" if action == "approve_compoff" else "reject") +
            " comp-off requests."), None

    ctx = pending_context or {}
    guid = ctx.get("compoff_guid") or _extract_guid(message)

    if not guid:
        # Approve: also surface previously-REJECTED requests, so a manager
        # who changes their mind can still approve them. Reject only makes
        # sense on currently-Requested ones.
        status_codes = ([_STATUS["requested"], _STATUS["rejected"]]
                        if action == "approve_compoff" else _STATUS["requested"])
        picker = _team_compoff_picker(user, token, action, status_codes)
        if picker is None:
            return _error_response(
                "There are no comp-off requests from your team right now."), None
        return picker, None

    # 90-day expiry guard — check EARLY (before asking about partial days).
    rec = _lookup_compoff_by_guid(guid, user, token)
    if action == "approve_compoff" and rec and _is_expired(rec):
        return _error_response(
            "This comp-off request expired on " +
            str(rec.get("expiry_date", ""))[:10] +
            " and can no longer be approved."), None

    # ---- PARTIAL APPROVAL for multi-day requests ----
    # A single-day request approves as-is. A multi-day request lets the
    # manager approve fewer days than requested (or all of them) by picking
    # the actual date range to approve, bounded within the original request.
    approved_from = ctx.get("approved_from")
    approved_to = ctx.get("approved_to")
    if action == "approve_compoff" and not approved_from and rec:
        orig_fr = str(rec.get("from_date", ""))[:10]
        orig_to = str(rec.get("to_date", ""))[:10]
        is_multi_day = bool(orig_fr and orig_to and orig_fr != orig_to)
        if is_multi_day:
            return json.dumps({
                "type": "date_picker",
                "hide_half_day": True,
                "message": "This is a multi-day comp-off request (" + orig_fr +
                          " to " + orig_to + "). Select how many day(s) you "
                          "want to approve (pick the full range to approve "
                          "all of it):",
                "min_date": orig_fr, "max_date": orig_to,
                "default_from": orig_fr, "default_to": orig_to,
                "context": {"action": "approve_compoff", "compoff_guid": guid},
            }), None
        # single-day request — approve the whole (only) day, no need to ask
        approved_from, approved_to = orig_fr, orig_to

    # need a reason/remark before actioning
    reason = ctx.get("reason") or extract_reason_from_message(message)
    if not reason:
        _prompt = ("Please add a comment for approving this comp-off:"
                  if action == "approve_compoff"
                  else "Please add a reason for rejecting this comp-off:")
        return _reason_picker_response(
            _prompt,
            {"_reason_submit": True, "action": action,
             "compoff_guid": guid,
             "approved_from": approved_from, "approved_to": approved_to}), None

    net_action = "approve_compoff" if action == "approve_compoff" else "reject_compoff"
    _payload = {"compoff_guid": guid, "comments": reason}
    if action == "approve_compoff" and approved_from and approved_to:
        _payload["approved_from"] = approved_from
        _payload["approved_to"] = approved_to
    response = call_hrbuddy_api(
        endpoint="/api/hrbuddy/execute-action",
        token=token, user=user, method="POST",
        body={"action": net_action, "payload": _payload})
    if not response.get("success"):
        return _error_response(response.get("message",
                               "Failed to process the comp-off request.")), None
    verb_done = "approved" if action == "approve_compoff" else "rejected"
    _days_note = ""
    if action == "approve_compoff" and approved_from:
        _days_note = " (" + approved_from + (
            " to " + approved_to if approved_to != approved_from else "") + ")"
    return _success_response(
        "\u2705 Comp-off request " + verb_done + _days_note + "."), None


def _extract_guid(message):
    m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                 r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", message or "")
    return m.group(0) if m else ""


def _lookup_compoff_by_guid(guid, user, token):
    try:
        q = build_dynamic_query(entity_name="compoff",
                                filters={"target": "single", "compoff_guid": guid},
                                current_user=user)
        data = execute_crm_query(crm_query=q, token=token, user=user)
        recs = data.get("data", []) if data.get("success") else []
        return recs[0] if recs else None
    except Exception:
        return None