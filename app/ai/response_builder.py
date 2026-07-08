import json
import re
import datetime as _dt
import ollama

from app.crm.entity_registry import ENTITY_REGISTRY, STATUS_MAPPING
from ollama import Client
OLLAMA_HOST = "http://127.0.0.1:11434"
client = Client(host=OLLAMA_HOST)
# Build a global code->word status map from the registry (per-module status_map
# entries) plus the global STATUS_MAPPING, so any module's status renders as a
# word with no extra code. New modules add a "status_map" in the registry only.
def _build_status_text():
    out = {}
    # global mapping is name->code; invert to code->Title
    for name, code in (STATUS_MAPPING or {}).items():
        out.setdefault(code, str(name).title())
    for _ent, _cfg in ENTITY_REGISTRY.items():
        for code, word in (_cfg.get("status_map") or {}).items():
            out[code] = word
    return out


_REGISTRY_STATUS_TEXT = _build_status_text()


# Build a global FIELD-NAME -> {code: label} map from every module's optional
# "value_maps". Lets the generic formatter render optionset codes (e.g.
# attendance marked_by 810100002 -> "Office Hours") as labels. Keys normalised
# to str so int/str codes both match. New modules add "value_maps" in registry.
def _build_value_maps():
    out = {}
    for _ent, _cfg in ENTITY_REGISTRY.items():
        for field, mapping in (_cfg.get("value_maps") or {}).items():
            field_l = field.lower()
            out.setdefault(field_l, {})
            for code, label in mapping.items():
                out[field_l][str(code)] = label
    return out


_REGISTRY_VALUE_MAPS = _build_value_maps()


RESPONSE_MODEL = "qwen2.5:1.5b"
# RESPONSE_MODEL = "qwen2.5:3b"

RESPONSE_PROMPT = """
You are HRBuddy AI.

Convert system results into a natural conversational response.

Rules:

- Never mention JSON.
- Never mention CRM.
- Never mention API.
- Never mention database.

- Use only supplied data.
- Never invent information.
- Do not write long paragraphs.
- Do not explain what you are doing.
- Prefer bullets and numbered records.
- For multiple records, output one numbered item per record.
- For each record, show only valid supplied fields.
- Do not ask the user to specify an exact record when the user asked for a list.
- Do not show internal GUID fields unless no other identifier exists.

- If records exist:
  show the records clearly.

- If records do not exist:
  politely explain that no matching information was found.

- Keep responses concise.
- Sound professional and helpful.
- Mixed Hinglish is allowed. Do not force full formal English.

If record_count is 0:
Use the user's query and intent to explain naturally what information could not be found.

Do not say:
"No data found"

IMPORTANT:

Only use information present in supplied records.

Never mention, infer, guess, summarize, explain, or reference
records that are not present in the provided dataset.

If the user requested exclusion filters,
assume excluded records do not exist.

Your entire response must be based only on the records received.

Instead explain naturally.
"""


HIDDEN_KEYS = {
    "employee_guid",
    "leave_guid",
    "leave_type_guid",
    "leave_structure_guid",
    "manager_guid",
}

FIELD_LABELS = {
    "employee_name": "Name",
    "employee_code": "Code",
    "department": "Department",
    "designation": "Designation",
    "experience": "Experience",
    "leave_type": "Leave Type",
    "balance": "Balance",
    "from_date": "From",
    "to_date": "To",
    "days": "Days",
    "status": "Status",
    "manager": "Manager",
    "reason": "Reason",
}


def _is_empty(value):
    return value in (None, "", [], {})


# Status codes -> words (used by the generic value formatter, keyed on the
# field NAME 'status', so any module returning a 'status' field renders nicely).
_STATUS_TEXT = {
    1: "Requested",
    100010001: "Approved",
    100010004: "Rejected",
    100010003: "Cancelled",
}


def _fmt_num(value, unit="days"):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    n = int(v) if v == int(v) else round(v, 1)
    word = unit if abs(n) == 1 and unit == "days" else unit
    if unit == "days":
        word = "day" if n == 1 else "days"
    return f"{n} {word}"


def _generic_value(key, value):
    """Format a value based on its field NAME (not entity), so new modules
    render correctly without code changes."""
    if value is None or value == "":
        return "-"
    k = key.lower()
    # per-field optionset code -> label (e.g. attendance marked_by)
    if k in _REGISTRY_VALUE_MAPS:
        return _REGISTRY_VALUE_MAPS[k].get(str(value), str(value))
    if k == "status":
        return _REGISTRY_STATUS_TEXT.get(value, str(value))
    if k.endswith("date") or k in ("from", "to"):
        return str(value)[:10]
    if k in ("balance", "days", "remaining", "used", "available", "leaves"):
        return _fmt_num(value)
    return str(value)


def _display(value):
    if value is None:
        return "-"
    return str(value)


def _label(key):
    return FIELD_LABELS.get(key, key.replace("_", " ").title())


def _visible_fields(record: dict):
    """Non-empty fields that aren't internal identifiers."""
    out = []
    for key, value in record.items():
        if key in HIDDEN_KEYS or key.endswith("_guid") or key.endswith("_id"):
            continue
        if _is_empty(value):
            continue
        out.append((key, value))
    return out


def format_generic(decision: dict, records: list):
    """Entity-agnostic clean formatter. Single record -> labeled list;
    multiple -> numbered, one compact line each. Works for ANY module whose
    backend returns the standard {data: [...]} shape — no per-module code."""
    if not records:
        return "No matching records were found."

    # Single record -> labelled key/value list
    if len(records) == 1 and not _is_list_request(decision, records):
        rec = records[0]
        lines = []
        for key, value in _visible_fields(rec):
            lines.append(f"- {_label(key)}: {_generic_value(key, value)}")
        return "\n".join(lines).strip() or "No details available."

    # Multiple records -> numbered, compact one-liners
    lines = [f"Records found: {len(records)}", ""]
    for i, rec in enumerate(records, start=1):
        fields = _visible_fields(rec)
        # First field is the "title" (name/code), rest joined with · separators
        parts = [_generic_value(k, v) for k, v in fields]
        lines.append(f"{i}. " + " · ".join(parts))
    return "\n".join(lines).strip()


def _is_list_request(decision: dict, records: list):
    filters = decision.get("filters", {}) or {}
    return (
        decision.get("target") == "multiple"
        or filters.get("target") == "multiple"
        or len(records) > 1
    )


def stream_text(text, chunk_size=300):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


def format_records_response(user_message: str, decision: dict, records: list):
    entity = decision.get("entity", "")
    filters = decision.get("filters", {}) or {}

    if not records:
        if entity == "employee":
            return "Koi matching employee record nahi mila."
        return "Koi matching record nahi mila."

    lines = []

    if entity == "employee":
        if _is_list_request(decision, records):
            lines.append(f"Employees found: {len(records)}")
            lines.append("")

            for index, record in enumerate(records, start=1):
                name = record.get("employee_name") or "Employee"
                code = record.get("employee_code")

                lines.append(f"Employee {index}")

                if code:
                    lines.append(f"Name: {name} ({code})")
                else:
                    lines.append(f"Name: {name}")

                if record.get("department"):
                    lines.append(f"Department: {record['department']}")

                if record.get("designation"):
                    lines.append(f"Designation: {record['designation']}")

                if record.get("experience") is not None:
                    lines.append(f"Experience: {record['experience']}")

                if index < len(records):
                    lines.append("")
            return "\n".join(lines).strip()

        record = records[0]
        name = (
            record.get("employee_name")
            or filters.get("resolved_employee_name")
            or filters.get("employee_name")
            or "Employee"
        )
        lines.append(f"{name} details:")
        lines.append("")

        for key, value in record.items():
            if key in HIDDEN_KEYS:
                continue
            if not _is_empty(value):
                lines.append(f"- {_label(key)}: {_display(value)}")

        return "\n".join(lines).strip()

    if _is_list_request(decision, records):
        lines.append(f"Records found: {len(records)}")
        lines.append("")

    for index, record in enumerate(records, start=1):
        if len(records) > 1:
            lines.append(f"{index}. Record")

        for key, value in record.items():
            if key in HIDDEN_KEYS or key.endswith("_guid") or key.endswith("_id"):
                continue
            if not _is_empty(value):
                prefix = "   - " if len(records) > 1 else "- "
                lines.append(f"{prefix}{_label(key)}: {_display(value)}")

        if index < len(records):
            lines.append("")

    return "\n".join(lines).strip()


def _fmt_num(value, unit="days"):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    n = int(v) if v == int(v) else round(v, 1)
    return f"{n} {unit}"


def _fmt_date(value):
    # backend sends ISO-ish strings; keep the date part only
    return str(value)[:10] if value else "-"


def _status_text(value):
    if value in (None, ""):
        return "-"
    return _REGISTRY_STATUS_TEXT.get(value, str(value))


def format_leave_balance(records: list):
    """Clean, basic leave-balance output. No prose."""
    rows = []
    for r in records:
        lt = r.get("leave_type") or "Leave"
        bal = r.get("balance")
        if bal in (None, ""):
            continue
        rows.append(f"- {lt}: {_fmt_num(bal)}")
    if not rows:
        return "No leave balance information is available right now."
    return "Leave Balance\n" + "\n".join(rows)


def format_leave_history(records: list):
    """One tidy line per leave record. No prose."""
    lines = [f"Leave History ({len(records)})", ""]
    for i, r in enumerate(records, start=1):
        lt = r.get("leave_type") or "Leave"
        frm = _fmt_date(r.get("from_date"))
        to = _fmt_date(r.get("to_date"))
        days = r.get("days")
        status = _status_text(r.get("status"))

        parts = [lt]
        if frm != "-" and to != "-":
            parts.append(f"{frm} → {to}")
        elif frm != "-":
            parts.append(frm)
        if days not in (None, ""):
            parts.append(_fmt_num(days))
        if status != "-":
            parts.append(status)

        lines.append(f"{i}. " + " · ".join(parts))
    return "\n".join(lines).strip()


# Optional per-entity overrides for cases where a custom shape reads better
# than the generic output. ANY entity NOT listed here falls back to the
# entity-agnostic format_generic() — so new modules need zero code changes.
def _override_employee(decision, records):
    return format_records_response("", decision, records)


def _override_leave(decision, records):
    return format_leave_balance(records) if records \
        else "No leave balance information was found."


def _override_leave_history(decision, records):
    return format_leave_history(records) if records \
        else "No leave records were found for that request."


# Named override formatters. The registry's per-module "formatter" field
# selects one of these by name; anything else (or "generic"/missing) uses the
# entity-agnostic format_generic(). New modules just set "formatter":"generic"
# (or omit it) in the registry and need ZERO code here.
SPECIAL_FORMATTERS = {
    "employee": _override_employee,
    "leave": _override_leave,
    "leave_history": _override_leave_history,
}


_SMART_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def _requested_leave_type(msg):
    """If the message names exactly ONE leave type (typo-tolerant), return its
    canonical keyword. 'anual'->annual, 'siick'->sick, etc."""
    import difflib
    msg = (msg or "").lower()
    canon = {"annual": "annual", "sick": "sick", "casual": "casual",
             "comp": "comp", "compoff": "comp", "carry": "carry",
             "earned": "earned", "privilege": "privilege",
             "maternity": "maternity", "paternity": "paternity"}
    targets = list(canon.keys())
    found = set()
    for tok in re.findall(r"[a-z]+", msg):
        if len(tok) < 4:
            continue
        m = difflib.get_close_matches(tok, targets, n=1, cutoff=0.8)
        if m:
            found.add(canon[m[0]])
    return next(iter(found)) if len(found) == 1 else ""


def _smart_parse_date(s):
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "")).date()
    except Exception:
        try:
            return _dt.datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _smart_phrase(base, question):
    """Rephrase a computed answer into one natural, friendly sentence. Numbers
    come from `base` (computed in Python), so they stay correct. Any failure ->
    return `base` unchanged. Never raises."""
    try:
        resp = ollama.chat(
            model=RESPONSE_MODEL,
            keep_alive="30m",
            messages=[
                {"role": "system", "content": (
                    "You rephrase an HR answer into ONE short, friendly, natural "
                    "sentence for a chat assistant. Keep ALL facts, names and "
                    "numbers EXACTLY as given — never change or invent numbers. "
                    "Do not add new information. Do not ask questions. Reply with "
                    "the sentence only, plain text, no quotes."
                )},
                {"role": "user", "content":
                    f"User asked: {question}\nAnswer to rephrase: {base}"},
            ],
            options={"temperature": 0.6, "num_predict": 70},
        )
        out = (resp.get("message", {}).get("content") or "").strip().strip('"')
        if not out or len(out) > len(base) * 3 + 60:
            return base
        base_nums = set(re.findall(r"\d+(?:\.\d+)?", base))
        out_nums = set(re.findall(r"\d+(?:\.\d+)?", out))
        # every base number must survive...
        if not base_nums.issubset(out_nums):
            return base
        # ...and the rephrase must NOT invent any new number (this is what made
        # "8 days remaining" become a false "can take 10 days").
        if not out_nums.issubset(base_nums):
            return base
        # preserve a leading capitalised name (no subject swap)
        m = re.match(r"([A-Z][a-zA-Z]+)'s|^([A-Z][a-zA-Z]+) ", base)
        nm = (m.group(1) or m.group(2)) if m else ""
        if nm and nm.lower() not in out.lower():
            return base
        return out
    except Exception:
        pass
    return base


def _smart_answer(user_message, decision, records):
    """Interpretive answer + natural phrasing. Returns None for normal/list
    queries (caller then shows the usual display)."""
    base = _smart_answer_raw(user_message, decision, records)
    if not base:
        return None
    return _smart_phrase(base, user_message or "")


def _smart_answer_raw(user_message, decision, records):
    try:
        entity = (decision or {}).get("entity", "")
        msg = (user_message or "").lower()
        if not records:
            return None

        # "who is manager of X" -> show that one person's manager
        if entity == "employee":
            f = (decision or {}).get("filters", {}) or {}
            # "what is my name" / "who am I" -> the user's own record
            if f.get("want_self_name") or re.search(r"my name|who am i|mera naam", msg):
                nm = " ".join((records[0].get("employee_name") or "").split())
                if nm:
                    return f"You are {nm.title() if nm.isupper() else nm}."
                return None
            wants_mgr = f.get("want_manager") or bool(
                re.search(r"manager of|who manages|'?s manager", msg))
            if wants_mgr and len(records) >= 1:
                r = records[0]
                who = (r.get("employee_name") or "This employee").title() \
                    if (r.get("employee_name") or "").isupper() \
                    else (r.get("employee_name") or "This employee")
                mgr = (r.get("manager") or "").strip()
                if mgr:
                    return f"{who}'s manager is {mgr.title() if mgr.isupper() else mgr}."
                return f"No manager is listed for {who}."
            return None

        # specific leave-type balance: "annual leave balance"
        if entity == "leave":
            want = _requested_leave_type(msg)
            # subject: a named person ("Purav") or the logged-in user ("you")
            who = " ".join(((decision or {}).get("filters", {}) or {})
                           .get("employee_name", "").split())
            who = who.title() if who else ""
            subj = who if who else "You"
            subj_low = who if who else "you"
            verb_have = "has" if who else "have"
            # --- balance INSIGHTS (computed in Python, accurate) ---
            num_m = re.search(r"(\d+(?:\.\d+)?)", msg)
            n_days = float(num_m.group(1)) if num_m else None

            def _bal_of(typ):
                for r in records:
                    if typ in (r.get("leave_type") or "").lower():
                        try:
                            return float(r.get("balance"))
                        except (TypeError, ValueError):
                            return None
                return None

            # "if I take N days [of <type>], what's my balance"
            if n_days is not None and ("if i take" in msg or "after taking" in msg
                                       or "after i take" in msg
                                       or re.search(r"\bif\b.*\btake", msg)):
                typ = want or "annual"
                cur = _bal_of(typ)
                if cur is not None:
                    left = cur - n_days
                    tname = typ.title() + (" Leave" if typ in ("annual", "sick",
                            "casual", "earned", "privilege") else "")
                    take_v = "takes" if who else "take"
                    will = (who + " will") if who else "you'll"
                    warn = "" if left >= 0 else " — that's more than available!"
                    return (f"If {subj_low} {take_v} {_fmt_num(n_days)} of {tname}, "
                            f"{will} have {_fmt_num(left)} left{warn}.")

            # "can I take N days" / "can purav take N" / "enough for N"
            if n_days is not None and ("can i take" in msg or "enough" in msg
                                       or "possible" in msg
                                       or re.search(r"\bcan\b.*\btake", msg)):
                typ = want or "annual"
                cur = _bal_of(typ)
                if cur is not None:
                    tname = typ.title()
                    if cur >= n_days:
                        return (f"Yes — {subj_low} {verb_have} {_fmt_num(cur)} of "
                                f"{tname} leave, enough for {_fmt_num(n_days)}.")
                    return (f"No — {subj_low} {verb_have} only {_fmt_num(cur)} of "
                            f"{tname} leave, less than the {_fmt_num(n_days)} "
                            f"asked about.")

            # "close to exhausting" / "running low"
            if any(k in msg for k in ("exhaust", "close to", "running low",
                                      "running out", "low balance")):
                low = None
                for r in records:
                    try:
                        b = float(r.get("balance"))
                    except (TypeError, ValueError):
                        continue
                    if low is None or b < low[1]:
                        low = (r.get("leave_type") or "Leave", b)
                if low:
                    if low[1] <= 2:
                        return (f"Yes — {subj_low}{'’s' if who else 'r'} {low[0]} "
                                f"is running low at {_fmt_num(low[1])}.")
                    return (f"Not really — the lowest is {low[0]} at "
                            f"{_fmt_num(low[1])}, so there's room.")

            if not want:
                return None
            for r in records:
                lt = r.get("leave_type") or ""
                if want in lt.lower():
                    bal = r.get("balance")
                    if bal not in (None, ""):
                        return f"You have {_fmt_num(bal)} of {lt} remaining."
            return None

        # interpretive leave history: counts / month / type
        if entity == "leave_history":
            # Whose data is this? If a name was extracted, the records belong to
            # that person (resolved + filtered upstream) — phrase with the name,
            # never with the question's name on someone else's data.
            _who = ((decision or {}).get("filters", {}) or {}).get("employee_name", "").strip()
            _subj = _who.title() if _who else "You"
            _has = "has" if _who else "have"
            _takes = "takes" if _who else "take"

            # ---- usage INSIGHTS (group-by, computed in Python) ----
            def _by_type():
                agg = {}
                for r in records:
                    t = r.get("leave_type") or "Leave"
                    try:
                        d = float(r.get("days") or 0)
                    except (TypeError, ValueError):
                        d = 0
                    c, days = agg.get(t, (0, 0.0))
                    agg[t] = (c + 1, days + d)
                return agg

            def _by_month():
                agg = {}
                for r in records:
                    dt = _smart_parse_date(r.get("from_date"))
                    if not dt:
                        continue
                    try:
                        d = float(r.get("days") or 0)
                    except (TypeError, ValueError):
                        d = 0
                    c, days = agg.get(dt.month, (0, 0.0))
                    agg[dt.month] = (c + 1, days + d)
                return agg

            # most-used leave type
            if "most" in msg and any(k in msg for k in (
                    "type", "used", "use", "take", "taken", "leave")) \
                    and "month" not in msg:
                agg = _by_type()
                if agg:
                    top = max(agg.items(), key=lambda kv: kv[1][1])
                    t, (c, days) = top
                    return (f"{_subj} {_has} used {t} the most — "
                            f"{_fmt_num(days)} across {c} request(s).")

            # which month has the most leaves
            if ("month" in msg) and any(k in msg for k in ("most", "which",
                                        "how often", "trend")):
                agg = _by_month()
                if agg:
                    top = max(agg.items(), key=lambda kv: kv[1][1])
                    mnum, (c, days) = top
                    mname = [k for k, v in _SMART_MONTHS.items()
                             if v == mnum][0].title()
                    return (f"{_subj} {_takes} the most leave in {mname} — "
                            f"{_fmt_num(days)} across {c} request(s).")

            # average monthly usage
            if "average" in msg or "avg" in msg:
                months = _by_month()
                if months:
                    total_days = sum(v[1] for v in months.values())
                    avg = total_days / len(months)
                    return (f"On average, {_subj.lower() if _subj=='You' else _subj} {_takes} about "
                            f"{_fmt_num(round(avg, 1))} of leave per active month.")

            interpretive = any(k in msg for k in (
                "how many", "how much", "kitni", "kitne", "count", "total",
                "number of", "did i", "have i",
            ))
            if not interpretive:
                return None

            month = None
            for name, idx in _SMART_MONTHS.items():
                if re.search(r"\b" + name + r"\b", msg):
                    month = idx
                    break
            want_type = _requested_leave_type(msg)

            sel = []
            for r in records:
                if month:
                    d = _smart_parse_date(r.get("from_date"))
                    if not d or d.month != month:
                        continue
                if want_type and want_type not in (r.get("leave_type") or "").lower():
                    continue
                sel.append(r)

            cnt = len(sel)
            days = 0.0
            for r in sel:
                try:
                    days += float(r.get("days") or 0)
                except (TypeError, ValueError):
                    pass

            label = (want_type + " leave") if want_type else "leave(s)"
            when = ""
            if month:
                mn = [k for k, v in _SMART_MONTHS.items() if v == month][0].title()
                when = " in " + mn
            if cnt == 0:
                return f"{_subj} {('did not' if _who else 'did not')} take any {label}{when}." if _who else f"You didn't take any {label}{when}."
            _verb = "took"
            return f"{_subj} {_verb} {cnt} {label}{when}, totalling {_fmt_num(days)}."

        return None
    except Exception:
        return None


def render_response(decision: dict, records: list):
    """Single entry point. Formatter choice comes from the registry's
    per-module "formatter" field (single source of truth), falling back to
    the generic entity-agnostic formatter."""
    entity = (decision or {}).get("entity", "")
    cfg = ENTITY_REGISTRY.get(entity, {}) if entity else {}
    formatter_name = cfg.get("formatter", "generic")
    formatter = SPECIAL_FORMATTERS.get(formatter_name)
    if formatter:
        return formatter(decision, records)
    return format_generic(decision, records)


def build_ai_response(
    user_message: str,
    decision: dict,
    result: dict
):
    records = result.get("data", [])
    entity = decision.get("entity")

    _smart = _smart_answer(user_message, decision, records)
    if _smart:
        return _smart

    # Deterministic, basic formatting for ALL structured data — generic-first,
    # with per-entity overrides where a custom shape reads better. New modules
    # need no code here; they fall through to the generic formatter.
    if entity in SPECIAL_FORMATTERS or records or entity:
        return render_response(decision, records)

    payload = {
    "query": user_message,
    "intent": decision,
    "records": result.get("data", []),
    "employees": result.get(
        "employees",
        []
    ),
    "record_count": len(
        result.get("data", [])
    )
}

    try:

        response = client.chat(
            model=RESPONSE_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": RESPONSE_PROMPT
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        payload,
                        default=str
                    )
                }
            ],
            options={
                "temperature": 0,
                "num_predict": 400
            }
        )

        content = (
            response["message"]["content"]
            .strip()
        )

        if not content:
            return fallback_response(
                result.get("data", [])
            )

        return content

    except Exception as ex:

        print(
            "RESPONSE BUILDER ERROR:",
            str(ex)
        )

        return fallback_response(
            result.get("data", [])
        )

def build_ai_response_stream(
    user_message: str,
    decision: dict,
    result: dict
):
    records = result.get("data", [])
    entity = decision.get("entity")

    # Smart natural answer for interpretive queries (counts / specific balance /
    # "how many in January"). Falls through to the normal display otherwise.
    _smart = _smart_answer(user_message, decision, records)
    if _smart:
        yield from stream_text(_smart)
        return

    # Generic-first deterministic formatting (same as build_ai_response),
    # streamed in chunks. New modules need no code changes here.
    if entity in SPECIAL_FORMATTERS or records or entity:
        yield from stream_text(render_response(decision, records))
        return

    payload = {
        "query": user_message,
        "intent": decision,
        "records": result.get("data", []),
        "record_count": len(result.get("data", []))
    }

    response = ollama.chat(
        model=RESPONSE_MODEL,
        messages=[
            {
                "role": "system",
                "content": RESPONSE_PROMPT
            },
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    default=str
                )
            }
        ],
        options={
            "temperature": 0,
            "num_predict": 400
        }
    )

    content = (
        response["message"]["content"]
            .strip()
        )

    if not content:
            content = fallback_response(
                result.get("data", [])
            )

    yield from stream_text(content)
def fallback_response(records):

    if not records:
        return (
            "I couldn't find any matching information."
        )
    return format_records_response("", {"entity": ""}, records)