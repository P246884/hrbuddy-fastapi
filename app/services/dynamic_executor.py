import types
import json
import random
import re

from app.crm.crm_query_builder import build_dynamic_query
from app.crm.crm_executor import execute_crm_query
from app.crm.crm_formatter import format_records
from app.crm.entity_registry import ENTITY_REGISTRY
from app.crm.entity_resolver import resolve_employee
from app.intent.fast_intent import clean_text
from app.security.permission_engine import can_read_entity
from app.ai.response_builder import build_ai_response,build_ai_response_stream


# ---------------------------------------------------------------------------
# LIST PAGINATION
# Large plain lists (leave history, employee directory) are returned as a
# structured "list" response so the UI can show a count + the first few rows +
# a "Show more" button (the rest are revealed client-side, no extra request).
# Insight / count / balance / single-record answers are NOT paginated — they
# keep their normal short streamed reply.
# ---------------------------------------------------------------------------
_PAGE_SIZE = 5
_LIST_INSIGHT_WORDS = (
    "most", "average", "avg", "trend", "how many", "how much", "kitni",
    "kitne", "count", "total", "which month", "usage", "used the", "balance",
)
_LIST_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)


def _list_item(entity, record):
    """A structured item for a paginated list: a primary title, an optional
    status badge, and labelled fields — the UI renders these as a card."""
    if entity == "employee":
        fields = []
        if record.get("department"):
            fields.append(["Department", str(record.get("department"))])
        if record.get("designation"):
            fields.append(["Designation", str(record.get("designation"))])
        if record.get("experience") not in (None, ""):
            fields.append(["Experience", str(record.get("experience")) + " yrs"])
        if record.get("employee_code"):
            fields.append(["Code", str(record.get("employee_code"))])
        return {
            "primary": str(record.get("employee_name") or record.get("name") or "Employee"),
            "badge": "",
            "fields": fields,
        }

    if entity == "leave_history":
        cfg = ENTITY_REGISTRY.get("leave_history", {})
        status_map = cfg.get("status_map", {})
        fd = str(record.get("from_date") or "")[:10]
        td = str(record.get("to_date") or "")[:10]
        span = fd + (" → " + td if td and td != fd else "")
        try:
            status_lbl = status_map.get(int(record.get("status")), str(record.get("status") or ""))
        except (TypeError, ValueError):
            status_lbl = str(record.get("status") or "")
        fields = []
        if span.strip():
            fields.append(["Dates", span])
        if record.get("days") not in (None, ""):
            fields.append(["Days", str(record.get("days"))])
        item = {
            "primary": str(record.get("leave_type") or "Leave"),
            "badge": status_lbl,
            "fields": fields,
        }
        att = record.get("attachment")
        if att and (att.get("documentbody") or att.get("filename")):
            item["attachment"] = {
                "filename": att.get("filename", "attachment"),
                "mimetype": att.get("mimetype", "application/octet-stream"),
                "documentbody": att.get("documentbody", ""),
            }
        return item

    # generic fallback
    fields = [[k, str(v)] for k, v in record.items() if v not in (None, "")][:4]
    return {"primary": fields[0][1] if fields else "Record", "badge": "", "fields": fields[1:]}


def _page_size(total):
    """Dynamic chunk size so the user clicks 'Show more' as little as possible:
    <=10 -> show everything at once; otherwise ~half the list, capped at 25 so
    very large lists come in 25-row chunks. (e.g. 22->11, 27->14, 70->25)."""
    if total <= 10:
        return total
    return min((total + 1) // 2, 25)


def _list_intro(entity, target, filters, count):
    """A short, natural, subject-aware header line. Knows WHOSE data it is:
    'your' for self, '<Name>'s' when HR/manager views someone else."""
    name = (filters.get("resolved_employee_name") or "").strip()
    name = " ".join(name.split())
    if name.isupper():
        name = name.title()

    if entity == "leave_history":
        if target == "self" or not name:
            return random.choice([
                "Here are your leave records (" + str(count) + ").",
                "Here's your leave history — " + str(count) + " record(s).",
                "Found " + str(count) + " leave record(s) for you.",
            ])
        return random.choice([
            "Here are " + name + "'s leave records (" + str(count) + ").",
            "Here's " + name + "'s leave history — " + str(count) + " record(s).",
            "Found " + str(count) + " leave record(s) for " + name + ".",
        ])

    if entity == "employee":
        dept = filters.get("department")
        if dept:
            d = str(dept).title()
            return random.choice([
                "Here are the " + str(count) + " employees in " + d + ".",
                "Found " + str(count) + " employees in " + d + ".",
            ])
        return random.choice([
            "Here are " + str(count) + " employees.",
            "Found " + str(count) + " employees in the directory.",
            "Showing all " + str(count) + " employees.",
        ])

    return str(count) + " records"


def _maybe_paginate_list(decision, entity, target, records):
    """Return a JSON 'list' response string for a list read, else None."""
    if not records:
        return None
    msg = (decision.get("original_message", "") or "").lower()
    is_employee_list = (entity == "employee" and target == "multiple")
    is_history_list = (entity == "leave_history")
    if not (is_employee_list or is_history_list):
        return None

    filters = decision.get("filters", {}) or {}

    # Leave SUMMARY: aggregate the user's leaves (approved days by type +
    # counts by status) and show it as the intro, followed by the full list.
    # Runs BEFORE the insight-word short-circuit so "summary" still lists.
    if is_history_list and filters.get("summary"):
        cfg = ENTITY_REGISTRY.get("leave_history", {})
        status_map = cfg.get("status_map", {}) or {}
        by_type = {}
        by_status = {}
        total_approved = 0.0
        for r in records:
            try:
                lbl = status_map.get(int(r.get("status")), str(r.get("status") or ""))
            except (TypeError, ValueError):
                lbl = str(r.get("status") or "")
            lbl = (lbl or "Unknown").title()
            by_status[lbl] = by_status.get(lbl, 0) + 1
            try:
                d = float(r.get("days") or 0)
            except (TypeError, ValueError):
                d = 0.0
            if lbl.lower() == "approved":
                t = str(r.get("leave_type") or "Leave")
                by_type[t] = by_type.get(t, 0) + d
                total_approved += d

        def _fmt(n):
            return str(int(n)) if float(n).is_integer() else str(n)

        parts = []
        if by_type:
            parts.append(", ".join(f"{k} {_fmt(v)}" for k, v in
                                   sorted(by_type.items(), key=lambda x: -x[1])))
        status_bits = ", ".join(f"{v} {k.lower()}" for k, v in
                                sorted(by_status.items(), key=lambda x: -x[1]))
        _who = (filters.get("resolved_employee_name")
                or filters.get("employee_name") or "").strip()
        if _who.isupper():
            _who = _who.title()
        if target != "self" and _who:
            lead = f"{_who}'s leave summary — {_who} has taken"
        else:
            lead = "Leave summary — you've taken"
        intro = (f"{lead} {_fmt(total_approved)} approved "
                 f"day(s)" + (f" ({parts[0]})" if parts else "") + ". "
                 f"Requests: {status_bits}.")
        items = [_list_item(entity, r) for r in records]
        return json.dumps({
            "type": "list", "kind": entity, "intro": intro,
            "count": len(records), "page_size": _page_size(len(records)),
            "items": items,
        })

    # insight / count / month / balance queries keep their short answer
    if any(w in msg for w in _LIST_INSIGHT_WORDS):
        return None

    intro = _list_intro(entity, target, filters, len(records))
    items = [_list_item(entity, r) for r in records]
    return json.dumps({
        "type": "list",
        "kind": entity,
        "intro": intro,
        "count": len(records),
        "page_size": _page_size(len(records)),
        "items": items,
    })


def _subject_label(filters, target, suffix):
    """'your <suffix>' for self, '<Name>'s <suffix>' when viewing someone else."""
    name = " ".join((filters.get("resolved_employee_name") or "").split())
    if name.isupper():
        name = name.title()
    if target != "self" and name:
        return name + "'s " + suffix
    return "your " + suffix


def _balance_response(decision, target, records):
    """One card per leave type with its remaining days (low balances flagged)."""
    filters = decision.get("filters", {}) or {}
    items = []
    for r in records:
        try:
            bal = float(r.get("balance"))
        except (TypeError, ValueError):
            bal = None
        items.append({"type": r.get("leave_type") or "Leave", "balance": bal})

    # If the user asked for ONE specific leave type, show only that. If they
    # have no such balance, say so instead of dumping every type.
    only = (filters.get("only_type") or "").strip().lower()
    if only:
        matched = [it for it in items
                   if only in str(it.get("type", "")).lower()
                   or str(it.get("type", "")).lower() in only]
        label = only.title()
        who = "you" if target == "self" else _subject_label(filters, target, "").strip() or "they"
        if not matched:
            msg = (f"You don't have any {label} balance available."
                   if target == "self"
                   else f"{who} has no {label} balance available.")
            return json.dumps({"type": "balance", "intro": msg, "items": []})
        intro = (f"Here's your {label} balance." if target == "self"
                 else f"Here's {who}'s {label} balance.")
        return json.dumps({"type": "balance", "intro": intro, "items": matched})

    intro = "Here are " + _subject_label(filters, target, "leave balances") + "."

    # Leave balance is a LIVE figure (current remaining) — there is no stored
    # per-year history and no way to project a future balance (leaves accrue /
    # reset, future applications are unknown). So for ANY year that isn't the
    # current one — or "last year" / "next year" — say so instead of passing off
    # today's numbers as that year's balance.
    import datetime as _dt
    _msg = (decision.get("original_message", "") or "").lower()
    _now = _dt.date.today().year
    _yrs = [int(y) for y in re.findall(r"\b(20\d{2})\b", _msg)]
    _other = [y for y in _yrs if y != _now]
    _past_word = bool(re.search(r"\blast year\b|\bprevious year\b|\bpichle saal\b", _msg))
    _future_word = bool(re.search(r"\bnext year\b|\bagle saal\b", _msg))
    if _other or _past_word or _future_word:
        _is_future = _future_word or any(y > _now for y in _other)
        _yr = (str(next((y for y in _other), "")) or
               ("next year" if _future_word else "last year"))
        if _is_future:
            intro = ("Leave balance is a live figure (your current remaining "
                     f"leaves) — I can't project what it'll be in {_yr}, since "
                     "that depends on future accrual, resets and any leaves you "
                     "take.")
        else:
            intro = ("Leave balance is a live figure (your current remaining "
                     f"leaves) — I don't have a stored balance for {_yr}. "
                     "For the leaves you actually took then, ask e.g. "
                     f"\"leave history {_yr}\".")
        # don't show the current numbers as if they were that year's balance
        return json.dumps({"type": "balance", "intro": intro, "items": []})
    return json.dumps({"type": "balance", "intro": intro, "items": items})


def _profile_response(decision, target, record):
    """A single employee profile rendered as a clean detail card."""
    filters = decision.get("filters", {}) or {}
    name = record.get("employee_name") or record.get("name") or "Employee"
    exp = record.get("experience")
    exp_str = ""
    if exp not in (None, ""):
        try:
            exp_str = (str(int(float(exp))) if float(exp).is_integer()
                       else str(float(exp))) + " yrs"
        except (TypeError, ValueError):
            exp_str = str(exp)
    raw = [
        ("Code", record.get("employee_code") or record.get("code")),
        ("Department", record.get("department")),
        ("Designation", record.get("designation")),
        ("Experience", exp_str),
        ("Manager", record.get("manager")),
    ]
    fields = [[k, str(v)] for k, v in raw if v not in (None, "", "None")]

    # Focused attribute: "who is my manager", "what is my department" -> return
    # just that one field, not the whole profile card.
    attr = (filters.get("attribute") or "").strip().lower()
    if attr:
        label_map = {"manager": "Manager", "department": "Department",
                     "designation": "Designation", "experience": "Experience",
                     "code": "Code"}
        want = label_map.get(attr)
        one = [f for f in fields if f[0] == want]
        who = "your" if target == "self" else (
            (name.title() if name.isupper() else name) + "'s")
        if one:
            val = one[0][1]
            subj = who.capitalize() if target == "self" else who
            import random as _rnd
            if attr == "manager":
                answer = _rnd.choice([
                    f"{subj} manager is {val}.",
                    f"{val} is {who} reporting manager.",
                    f"{subj} leaves go to {val} for approval.",
                    f"That'd be {val} — {who} manager.",
                ])
            elif attr == "department":
                answer = _rnd.choice([
                    f"{subj} department is {val}.",
                    f"{who.capitalize() if target=='self' else who} in the {val} department.",
                    f"{val} — that's {who} department.",
                ])
            elif attr == "designation":
                answer = _rnd.choice([
                    f"{subj} designation is {val}.",
                    f"{who.capitalize() if target=='self' else who} designated as {val}.",
                    f"{val} — that's {who} role.",
                ])
            elif attr == "experience":
                answer = _rnd.choice([
                    f"{subj} experience is {val}.",
                    f"{who.capitalize() if target=='self' else who} got {val} of experience.",
                    f"{val} of experience on record for {who if target!='self' else 'you'}.",
                ])
            else:
                answer = f"{subj} {want.lower()} is {val}."
            return json.dumps({"type": "profile", "intro": answer,
                               "name": name, "fields": one})
        # attribute not on record -> honest short answer, no full dump
        return json.dumps({"type": "profile",
                           "intro": f"{who.capitalize() if target=='self' else who} {attr} is not available.",
                           "name": name, "fields": []})

    intro = ("Here's your profile." if target == "self"
             else "Here's " + (name.title() if name.isupper() else name) + "'s profile.")
    return json.dumps({"type": "profile", "intro": intro,
                       "name": name, "fields": fields})


def _grouped_balance(decision, filters, user, token):
    """Fetch each named person's balance separately and return a per-person
    grouped response (the base for comparisons). Each group is one employee."""
    resolved = filters.get("resolved_employees", []) or []
    base = {k: v for k, v in filters.items()
            if k not in ("employee_guids", "employee_names", "resolved_employees",
                         "employee_guid", "resolved_employee_name")}
    groups = []
    for emp in resolved:
        guid = emp.get("employee_guid")
        nm = emp.get("employee_name") or emp.get("name") or "Employee"
        nm = nm.title() if nm.isupper() else nm
        if not guid:
            continue
        if not can_read_entity(entity="leave", current_user=user,
                               target_employee=guid, token=token):
            groups.append({"name": nm, "denied": True, "items": []})
            continue
        q = build_dynamic_query(
            entity_name="leave",
            filters={**base, "employee_guid": guid, "target": "employee"},
            current_user=user,
        )
        data = execute_crm_query(crm_query=q, token=token, user=user)
        recs = data.get("data", []) if data.get("success") else []
        items = []
        for r in recs:
            try:
                bal = float(r.get("balance"))
            except (TypeError, ValueError):
                bal = None
            items.append({"type": r.get("leave_type") or "Leave", "balance": bal})
        groups.append({"name": nm, "items": items})

    if not groups:
        return "No employees found."
    return json.dumps({
        "type": "balance_group",
        "intro": "Leave balance — " + ", ".join(g["name"] for g in groups),
        "groups": groups,
    })


def _structured_response(decision, entity, target, records):
    """Pick the prettiest structured card for a plain display read, else None."""
    if not records:
        return None
    if entity == "leave":
        return _balance_response(decision, target, records)
    if entity == "employee":
        # one person -> profile card (works for "my profile" and "X's profile");
        # several -> the list cards.
        if len(records) == 1 and target != "multiple":
            return _profile_response(decision, target, records[0])
        return _maybe_paginate_list(decision, entity, "multiple", records)
    return _maybe_paginate_list(decision, entity, target, records)

def _is_hr_or_admin(user: dict) -> bool:
    return bool(user.get("is_hr")) or bool(user.get("is_admin"))


def _resolve_lookup_guid(entity_name, search_name, name_field, token, user):
    """Lookup entity se naam ka GUID fetch karo. Exact (case-insensitive)
    name match ko prefer karta hai, warna pehla result."""
    if not search_name or str(search_name).strip() == "":
        return ""
    try:
        from app.api_clients.hrms_api import call_hrbuddy_api
        result = call_hrbuddy_api(
            endpoint="/api/hrbuddy/dynamic-query",
            token=token, user=user, method="POST",
            body={
                "crm_entity": entity_name,
                "crm_filters": {name_field + "_contains": search_name},
                "fields": {"guid": entity_name + "id", "name": name_field}
            }
        )
        if result.get("success") and result.get("data"):
            rows = result["data"]
            target = clean_text(search_name)
            for row in rows:
                if clean_text(str(row.get("name", ""))) == target:
                    return row.get("guid", "")
            return rows[0].get("guid", "")
    except Exception as e:
        print("Lookup resolve error:", e)
    return ""


def _same_person_name(employee_name: str, user: dict) -> bool:
    if not employee_name:
        return False

    return clean_text(employee_name) in clean_text(user.get("name", ""))


def _apply_local_type_filter(entity: str, filters: dict, records: list):
    filter_type = filters.get("type")
    filter_values = filters.get("types", []) or []

    if not filter_type or not filter_values:
        return records

    entity_config = ENTITY_REGISTRY.get(entity, {})
    field_name = entity_config.get("type_filter_field")

    if not field_name:
        return records
    print("FIELD NAME:", field_name)
    print("FILTER TYPE:", filter_type)
    print("FILTER VALUES:", filter_values)
    print("BEFORE FILTER:", records)

    normalized_values = [clean_text(value) for value in filter_values]

    if filter_type == "exclude":
        filtered = [
        item for item in records
        if not any(
            value in clean_text(
                str(item.get(field_name, ""))
            )
            for value in normalized_values
        )
    ]

        print("AFTER FILTER:", filtered)

        return filtered

    if filter_type == "include":
        filtered = [
        item for item in records
        if any(
            value in clean_text(
                str(item.get(field_name, ""))
            )
            for value in normalized_values
        )
    ]

        print("AFTER FILTER:", filtered)

        return filtered
    print(
    "FILTER DEBUG:",
    filter_type,
    filter_values,
    field_name
)
    return records


def _build_employee_choices(employee_records: list):
    response = "Multiple employees found:\n\n"

    for index, emp in enumerate(employee_records, start=1):
        response += (
            f"{index}. "
            f"{emp.get('employee_name')} | "
            f"{emp.get('employee_code')} | "
            f"{emp.get('department')} | "
            f"{emp.get('designation')}\n"
        )

    response += "\nPlease specify employee name or employee code."
    return response


def _resolve_target_employee(
    entity: str,
    target: str,
    filters: dict,
    user: dict,
    token: str
):
    employee_names = filters.get("employee_names", [])
    employee_name = filters.get("employee_name", "")
    employee_code = (filters.get("employee_code") or "").strip()
    if employee_names:

        resolved_employees = []

        for emp_name in employee_names:

            employee_result = resolve_employee(
                employee_name=emp_name,
                token=token,
                user=user
            )

            if not employee_result.get("success"):
                continue

            resolved_employees.extend(
                employee_result.get("data", [])
            )

        if not resolved_employees:
            return "No employees found."

        filters["resolved_employees"] = resolved_employees

        filters["employee_guids"] = [
            emp["employee_guid"]
            for emp in resolved_employees
        ]

        return None

    # If normal user asks for any named employee who is not clearly himself/herself,
    # deny before search to avoid leaking whether that person exists.
    if employee_name:
        # HR/Admin = full access
        # Others = allow through — can_read_entity will check manager relationship
        if (
            not _is_hr_or_admin(user)
            and not _same_person_name(employee_name, user)
        ):
            # Don't block here — let can_read_entity check manager relationship
            pass
    # If the typed name is the logged-in user's own name, treat as self.
    if _same_person_name(employee_name, user):
        filters["employee_guid"] = user.get("user_guid")
        filters["resolved_employee_name"] = user.get("name")
        filters["resolved_employee_code"] = ""
        return None

    # ------------------------------------------------------------------
    # Resolve lookup filters to GUIDs.
    # bam_designation / bam_department / bam_manager are LookupType fields,
    # so CRM can only filter them by GUID (text -> Guid parse error).
    # We resolve the human-readable name to a GUID here; the query builder
    # then filters by that GUID.
    #   Designation -> cor_designation (name field: cor_name)
    #   Department  -> cor_department  (name field: cor_name)
    #   Manager     -> bam_employee    (a manager is an employee)
    # If a name was given but nothing resolved, return a clear message
    # instead of silently dropping the filter (which would return everyone).
    # ------------------------------------------------------------------

    # Single designation -> GUID
    if filters.get("designation") and not filters.get("designation_guid"):
        guid = _resolve_lookup_guid(
            "cor_designation", filters["designation"], "cor_name", token, user
        )
        if guid:
            filters["designation_guid"] = guid
            print("DESIGNATION GUID:", guid)
        else:
            return f"No designation found matching '{filters['designation']}'."

    # Multiple designations (e.g. "team member or intern") -> list of GUIDs
    if filters.get("designations") and not filters.get("designation_guids"):
        guids = []
        for name in filters["designations"]:
            g = _resolve_lookup_guid(
                "cor_designation", name, "cor_name", token, user
            )
            if g:
                guids.append(g)
        if guids:
            filters["designation_guids"] = guids
            print("DESIGNATION GUIDS:", guids)
        else:
            return (
                "No matching designations found for "
                f"{filters['designations']}."
            )

    # Department -> GUID
    if filters.get("department") and not filters.get("department_guid"):
        guid = _resolve_lookup_guid(
            "cor_department", filters["department"], "cor_name", token, user
        )
        if guid:
            filters["department_guid"] = guid
            print("DEPARTMENT GUID:", guid)
        else:
            return f"No department found matching '{filters['department']}'."

    # Multiple departments (e.g. "project or finance") -> list of GUIDs
    if filters.get("departments") and not filters.get("department_guids"):
        dept_guids = []
        for name in filters["departments"]:
            g = _resolve_lookup_guid(
                "cor_department", name, "cor_name", token, user
            )
            if g:
                dept_guids.append(g)
        if dept_guids:
            filters["department_guids"] = dept_guids
            print("DEPARTMENT GUIDS:", dept_guids)
        else:
            return (
                "No matching departments found for "
                f"{filters['departments']}."
            )

    # Manager (a manager is itself an employee) -> employee GUID
    if filters.get("manager") and not filters.get("manager_guid"):
        mgr_result = resolve_employee(
            employee_name=filters["manager"], token=token, user=user
        )
        mgr_records = mgr_result.get("data", []) if mgr_result.get("success") else []
        if len(mgr_records) == 1:
            filters["manager_guid"] = mgr_records[0].get("employee_guid")
        elif len(mgr_records) > 1:
            exact = [
                r for r in mgr_records
                if clean_text(r.get("employee_name", "")) == clean_text(filters["manager"])
            ]
            if len(exact) == 1:
                filters["manager_guid"] = exact[0].get("employee_guid")
            else:
                return _build_employee_choices(mgr_records)
        else:
            return f"No manager found matching '{filters['manager']}'."
        print("MANAGER GUID:", filters.get("manager_guid"))

    # Multiple managers (e.g. "manager is shashank or rahul") -> list of GUIDs.
    # Per-name: take the single match, or the exact-name match if ambiguous;
    # skip a name only if it can't be resolved unambiguously.
    if filters.get("managers") and not filters.get("manager_guids"):
        mgr_guids = []
        for name in filters["managers"]:
            res = resolve_employee(employee_name=name, token=token, user=user)
            recs = res.get("data", []) if res.get("success") else []
            if len(recs) == 1:
                mgr_guids.append(recs[0].get("employee_guid"))
            elif len(recs) > 1:
                exact = [
                    r for r in recs
                    if clean_text(r.get("employee_name", "")) == clean_text(name)
                ]
                if len(exact) == 1:
                    mgr_guids.append(exact[0].get("employee_guid"))
        mgr_guids = [g for g in mgr_guids if g]
        if mgr_guids:
            filters["manager_guids"] = mgr_guids
            print("MANAGER GUIDS:", mgr_guids)
        else:
            return f"No matching managers found for {filters['managers']}."

    # Self query — employee_name empty hai toh user_guid se fetch karo
    # LEKIN agar koi search filter hai toh self-resolve mat karo
    has_search_filter = (
        filters.get("starts_with") or
        filters.get("designation") or
        filters.get("designations") or
        filters.get("department") or
        filters.get("departments") or
        filters.get("manager") or
        filters.get("managers") or
        filters.get("experience_gt") not in (None, "", []) or
        filters.get("experience_gte") not in (None, "", []) or
        filters.get("experience_lt") not in (None, "", []) or
        filters.get("experience_lte") not in (None, "", []) or
        target == "multiple"
    )

    if not employee_name and not has_search_filter and not employee_code:
        if user.get("user_guid"):
            filters["employee_guid"] = user.get("user_guid")
            filters["resolved_employee_name"] = user.get("name", "")
            filters["resolved_employee_code"] = ""
            return None
        return "Unable to identify current user."

    if not employee_name and has_search_filter and not employee_code:
        # Search filter hai — employee_guid set mat karo
        return None

    # ------------------------------------------------------------------
    # EMPLOYEE CODE (id) lookup — most reliable identifier. If the query gave
    # a code ("employee id 1214", "balance for 1214"), resolve by code first.
    # Auth for named/other employees is enforced later by can_read_entity.
    # ------------------------------------------------------------------
    if employee_code and not filters.get("employee_guid"):
        code_result = resolve_employee(
            employee_name="", token=token, user=user,
            employee_code=employee_code
        )
        if not code_result.get("success"):
            return "Unable to search employee."
        code_records = code_result.get("data", [])
        if not code_records:
            return f"No employee found with ID {employee_code}."
        emp0 = code_records[0]
        filters["employee_guid"] = emp0.get("employee_guid")
        filters["resolved_employee_name"] = emp0.get("employee_name")
        filters["resolved_employee_code"] = emp0.get("employee_code")
        return None

    employee_result = resolve_employee(
        employee_name=employee_name,
        token=token,
        user=user
    )

    if not employee_result.get("success"):
        return "Unable to search employee."

    employee_records = employee_result.get("data", [])

    if not employee_records:
        return f"No employee found with name {employee_name}."

    if len(employee_records) > 1:
        exact_matches = [
            emp for emp in employee_records
            if clean_text(emp.get("employee_name", "")) == clean_text(employee_name)
            or clean_text(emp.get("employee_code", "")) == clean_text(employee_name)
        ]

        if len(exact_matches) == 1:
            employee_records = exact_matches
        else:
            return _build_employee_choices(employee_records)

    employee_guid = employee_records[0].get("employee_guid")

    if not employee_guid:
        return "Unable to identify employee."

    filters["employee_guid"] = employee_guid
    filters["resolved_employee_name"] = employee_records[0].get("employee_name")
    filters["resolved_employee_code"] = employee_records[0].get("employee_code")

    return None

def fetch_data_only(
    decision: dict,
    user: dict,
    token: str
):
    entity = decision.get("entity")
    filters = decision.get("filters", {}) or {}
    target = decision.get("target", "self")

    resolve_error = _resolve_target_employee(
        entity=entity,
        target=target,
        filters=filters,
        user=user,
        token=token
    )
    
    if resolve_error:
        return {
            "success": False,
            "message": resolve_error
        }

    crm_query = build_dynamic_query(
        entity_name=entity,
        filters={**filters, "target": target},
        current_user=user
    )

    data = execute_crm_query(
        crm_query=crm_query,
        token=token,
        user=user
    )

    if not data.get("success"):
        return data

    records = data.get("data", [])
    
    records = _apply_local_type_filter(
        entity,
        filters,
        records
    )

    return {
        "success": True,
        "employee_name": filters.get(
            "resolved_employee_name",
            filters.get("employee_name")
        ),
        "records": records,
        "formatted_response": format_records(records)
    }

def execute(
    decision: dict,
    user: dict,
    token: str
):
    entity = decision.get("entity")
    operation = decision.get("operation", "read")
    filters = decision.get("filters", {}) or {}
    target = decision.get("target", "self")
    # ----------------------------------
    # MULTIPLE EMPLOYEE SUPPORT
    # ----------------------------------

     
    if not entity:
        return "I could not understand which HR information you need. Please ask with employee, leave, or leave history details."

    if entity not in ENTITY_REGISTRY:
        return f"{entity} is not configured yet."

    if operation != "read":
        return "This action is not supported yet."

    # Attendance: resolve the date window from the message. Future dates don't
    # exist, so short-circuit with a friendly message; otherwise inject the
    # date range (+ optional System/Office Hours) into the filters.
    if entity == "attendance":
        from app.services.attendance_window import resolve_attendance_window
        win = resolve_attendance_window(decision.get("original_message", "") or "")
        if win.get("error_message"):
            return win["error_message"]
        filters["date_from"] = win.get("date_from")
        filters["date_to"] = win.get("date_to")
        if win.get("marked_by"):
            filters["marked_by"] = win["marked_by"]

    # Resolve person name before building query.
    resolve_error = _resolve_target_employee(
        entity=entity,
        target=target,
        filters=filters,
        user=user,
        token=token
    )
    print("AFTER RESOLVE FILTERS:", filters)
    if resolve_error:
        return resolve_error

    # Multi-person LEAVE BALANCE ("purav and harshal balance") — fetch each
    # person separately so the cards are grouped per person (no jumbled merge).
    if entity == "leave" and len(filters.get("employee_guids", []) or []) > 1:
        return _grouped_balance(decision, filters, user, token)

    allowed = can_read_entity(
        entity=entity,
        current_user=user,
        target_employee=filters.get("employee_guid"),
        token=token
    )

    if not allowed:
        return "You are not authorized to view other employees' data."

    if target == "employee" and entity != "employee" and not filters.get("employee_guid"):
        return "Unable to identify employee."

    if target == "employee" and entity == "employee" and not filters.get("employee_guid") and not filters.get("employee_name"):
        return "Unable to identify employee."
    
    crm_query = build_dynamic_query(
        entity_name=entity,
        filters={**filters, "target": target},
        current_user=user
    )

    data = execute_crm_query(
        crm_query=crm_query,
        token=token,
        user=user
    )

    if not data.get("success"):

        print(
        "CRM FETCH ERROR:",
        data.get("message")
        )

        return (
                "Sorry, I was unable "
                "to fetch the requested "
                "HR data right now."
        )

    records = data.get("data", [])
    print("BEFORE FILTER:", records)

    records = _apply_local_type_filter(
        entity,
        filters,
        records
    )
    print("AFTER FILTER:", records)

    # If the user named a specific month ("...of July"), narrow leave_history
    # records to that month so "show my pending leaves of July" lists only
    # July's (and "how many ... in July" counts only July's).
    if entity == "leave_history" and records:
        _msg = (decision.get("original_message", "") or "").lower()
        _mnums = {i + 1 for i, m in enumerate(_LIST_MONTHS)
                  if re.search(r"\b" + m + r"\b", _msg)}
        if _mnums:
            def _rec_month(r):
                ds = (r.get("from_date") or "")
                try:
                    return int(ds[5:7])
                except (ValueError, IndexError):
                    return None
            filtered = [r for r in records if _rec_month(r) in _mnums]
            records = filtered

    # Attach any CRM note/attachment to each leave so the history cards can show
    # a download link. Best-effort + capped (one light query per leave), never
    # allowed to break the listing. Enrich most-recent-first so a freshly-applied
    # leave is always covered even in a long history.
    if entity == "leave_history" and records:
        try:
            from app.services.leave_action_executor import _attachment_for_leave
            order = sorted(
                range(len(records)),
                key=lambda i: str(records[i].get("from_date") or ""),
                reverse=True,
            )
            for i in order[:80]:
                att = _attachment_for_leave(records[i].get("leave_guid"),
                                            token, user, with_body=True)
                if att:
                    records[i]["attachment"] = att
        except Exception as _e:
            print("attachment enrich skipped:", _e)

    # 1) A specific question ("can I take 5?", "annual balance", "most used")
    #    deserves a short, computed sentence — not a card dump.
    try:
        from app.ai.response_builder import _smart_answer
        _smart = _smart_answer(decision.get("original_message", ""), decision, records)
    except Exception as _e:
        print("smart-answer skipped:", _e)
        _smart = None
    if _smart:
        print("SMART ANSWER:", _smart)
        return _smart

    # 2) Plain display -> prettiest structured card (balance / profile / list).
    structured = _structured_response(decision, entity, target, records)
    if structured is not None:
        print("STRUCTURED RESPONSE:", structured[:120])
        return structured

    # 3) Fallback: stream the formatted text.
    formatted_response = format_records(records)
    print("FINAL RECORDS:", records)
    print("FORMATTED:", formatted_response)
    response = build_ai_response_stream(
        user_message=decision.get("original_message", ""),
        decision=decision,
        result={
            "success": True,
            "data": records,
            "formatted_response": formatted_response
        }
    )
    print(type(response))
    return response