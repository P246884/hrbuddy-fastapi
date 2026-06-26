"""
HRBuddy deterministic-layer eval suite.

Runs WITHOUT Ollama or the CRM — it exercises the rule-based layer where almost
all of our bugs lived: entity routing, fast-intent parsing, employee name/code
extraction, multi-match disambiguation, action detection, relative dates and the
content guard.

Run it after ANY change to catch regressions in seconds:

    python tests/eval_suite.py

Exit code is 0 if everything passes, 1 if anything fails (CI-friendly).
Add a new case by appending a tuple to the relevant list below — no framework,
no boilerplate.
"""

import os
import sys
import types

# --- make the app importable + stub Ollama (we don't need a live model here) ---
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if "ollama" not in sys.modules:
    _fake = types.ModuleType("ollama")
    _fake.chat = lambda **k: {"message": {"content": ""}}
    _fake.Client = lambda *a, **k: types.SimpleNamespace(
        chat=lambda **k: {"message": {"content": ""}}
    )
    sys.modules["ollama"] = _fake

from app.intent.fast_intent import (
    resolve_entity,
    parse_fast_intent,
    should_use_llm,
    normalize_typos,
)
from app.services.leave_action_executor import (
    extract_employee_name_for_action,
    extract_employee_code_for_action,
    narrow_employee_records,
    extract_relative_date,
    parse_bulk_actions,
)
from app.agents.hr_agent import detect_action_intent
from app.security.content_guard import is_inappropriate
from app.services.dynamic_executor import _maybe_paginate_list, _page_size
from app.ai.response_builder import _smart_answer_raw
import json as _json


# Track results across all sections.
_TOTAL = {"pass": 0, "fail": 0}
_FAILURES = []


def check(section, label, got, want):
    ok = got == want
    _TOTAL["pass" if ok else "fail"] += 1
    if not ok:
        _FAILURES.append(f"[{section}] {label}\n      got : {got!r}\n      want: {want!r}")
    mark = "ok " if ok else "XX "
    print(f"  {mark} {label}")


def section(title):
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------
# 1) Entity routing  (leave balance vs leave_history vs employee)
# --------------------------------------------------------------------------
def test_entity_routing():
    section("entity routing")
    cases = [
        # balance
        ("show my leave balance", "leave"),
        ("remaining leaves", "leave"),
        ("remaining leaves of harshal", "leave"),
        ("show leave balance of harshal", "leave"),
        ("kitni leave bachi hai", "leave"),
        # history (status / history words win even with singular "leave")
        ("show leave history", "leave_history"),
        ("harshal's approved leaves", "leave_history"),
        ("harshal's leave in requested state ones", "leave_history"),
        ("show rejected leaves this month", "leave_history"),
        ("approved leaves", "leave_history"),
        ("applied leaves of purav", "leave_history"),
        # employee
        ("show my profile", "employee"),
        ("show all employees", "employee"),
        ("show employees in project department", "employee"),
    ]
    for msg, want in cases:
        check("entity", msg, resolve_entity(msg), want)


# --------------------------------------------------------------------------
# 2) Fast-intent target / filters / defer-to-Ollama
# --------------------------------------------------------------------------
def _fi(msg):
    """Return a compact (entity, target, key-filters) snapshot, or None."""
    d = parse_fast_intent(msg)
    if d is None:
        return None
    f = d["filters"]
    keys = {k: f[k] for k in ("starts_with", "designation", "department",
                              "employee_name") if f.get(k)}
    return (d["entity"], d["target"], keys)


def test_fast_intent():
    section("fast-intent routing")
    # list queries must be target=multiple (never silently 'self')
    check("fast", "show all employees -> multiple",
          _fi("show all employees"), ("employee", "multiple", {}))
    check("fast", "list employees -> multiple",
          _fi("list employees"), ("employee", "multiple", {}))
    # clean filters resolved fast
    check("fast", "starts with A",
          _fi("employees whose name starts with A"),
          ("employee", "multiple", {"starts_with": "A"}))
    check("fast", "department filter",
          _fi("show employees in project department"),
          ("employee", "multiple", {"department": "project"}))
    # self / single
    check("fast", "my profile -> self",
          _fi("show my profile"), ("employee", "self", {}))
    check("fast", "harshal profile -> employee",
          _fi("show harshal profile"), ("employee", "employee", {"employee_name": "Harshal"}))
    # subset it cannot resolve -> defer to Ollama (None), never dump all
    check("fast", "related-to-name -> contains filter (fast path)",
          (lambda d: d and d["entity"] == "employee"
           and d["filters"].get("employee_name_contains") == "Harsh")(
              parse_fast_intent("show employees related to name harsh")), True)
    # complex -> defer to Ollama
    check("fast", "experience -> fast path (not defer)",
          parse_fast_intent("employees whose experience is more than 4 years") is not None, True)
    check("fast", "manager defers",
          parse_fast_intent("employees whose manager is shashank"), None)


def test_should_use_llm():
    section("should_use_llm")
    # genuinely complex -> LLM
    for msg in ["compare project and finance department",
                "who is the manager of harshal",
                "show employees department wise"]:
        check("llm", f"complex: {msg}", should_use_llm(msg), True)
    # experience is now handled on the FAST path (not deferred)
    for msg in ["employees jinka experience 4 saal se jyada ho",
                "who all have experience more than 5 years",
                "show all employees", "show my profile",
                "employees in project department"]:
        check("llm", f"fast: {msg}", should_use_llm(msg), False)


# --------------------------------------------------------------------------
# 3) Action detection  (apply/approve/reject/cancel vs read)
# --------------------------------------------------------------------------
def test_action_detection():
    section("action detection")
    cases = [
        ("As I was out of city yesterday so please apply a leave", "apply_leave"),
        ("As harshal was out of city yesterday so please apply leave for him", "apply_leave"),
        ("mere kl ki leave apply krdo", "apply_leave"),
        ("approve harsh leave", "approve_leave"),
        ("reject harsh leave", "reject_leave"),
        ("cancel my leave", "cancel_leave"),
        # typo tolerance (fuzzy) — users mistype, we still understand
        ("appl sick leave", "apply_leave"),
        ("aply sick leave", "apply_leave"),
        ("apply sik", "apply_leave"),
        ("apprve harshal leave", "approve_leave"),
        ("rejct harsh leave", "reject_leave"),
        ("cancl my leave", "cancel_leave"),
        # "pending" describes the target leave -> still an action, not a read
        ("plz aprove harshal pending leave", "approve_leave"),
        ("approve harshal pending leave", "approve_leave"),
        # READ queries must NOT be detected as actions
        ("show my leave balance", None),
        ("remaining leaves", None),
        ("harshal's approved leaves", None),
        ("show leave history", None),
        ("show my applied leaves", None),
    ]
    for msg, want in cases:
        check("action", msg, detect_action_intent(msg), want)


# --------------------------------------------------------------------------
# 4) Employee name extraction for actions
# --------------------------------------------------------------------------
def test_name_extraction():
    section("name extraction (actions)")
    cases = [
        ("approve harsh leave", "Harsh"),
        ("reject harshal leaves", "Harshal"),
        ("approve harsh with code 1215 leave", "Harsh"),
        ("reject a leave for harsh", "Harsh"),
        ("approve a leave of harsh", "Harsh"),
        ("approve harshal's leave", "Harshal"),
        ("As harshal was out of city apply leave for him", "Harshal"),
        ("apply leave for purav tomorrow", "Purav"),
        # self / non-names
        ("approve my leave", ""),
        ("approve a leave", ""),
        ("mere prso ki leave apply krdo", ""),
        ("apply leave for me", ""),
    ]
    for msg, want in cases:
        check("name", msg, extract_employee_name_for_action(msg), want)


def test_code_extraction():
    section("code extraction (actions)")
    cases = [
        ("apply leave for harsh employee code 1215", "1215"),
        ("approve harsh with code 1215 leave", "1215"),
        ("apply leave for harsh (1216)", "1216"),
        ("apply leave for code IN10", "IN10"),
        ("approve harsh leave", ""),
    ]
    for msg, want in cases:
        check("code", msg, extract_employee_code_for_action(msg), want)


# --------------------------------------------------------------------------
# 5) Multi-match disambiguation (narrow by any clue)
# --------------------------------------------------------------------------
_RECS = [
    {"employee_name": "HARSHIT SHARMA", "employee_code": "IN10",
     "department": "Interns", "designation": "Intern"},
    {"employee_name": "HARSH", "employee_code": "1215",
     "department": "Project", "designation": "Team Member"},
    {"employee_name": "HARSHAL PATEL", "employee_code": "1216",
     "department": "Sales", "designation": "Manager"},
]


def _narrow_name(msg):
    out = narrow_employee_records([dict(r) for r in _RECS], msg)
    return out[0]["employee_name"] if len(out) == 1 else f"AMBIGUOUS({len(out)})"


def test_disambiguation():
    section("disambiguation (narrow by clue)")
    cases = [
        ("apply leave for harsh employee code 1215", "HARSH"),
        ("apply leave for harshal patel", "HARSHAL PATEL"),
        ("apply leave for patel", "HARSHAL PATEL"),
        ("apply leave for harshit", "HARSHIT SHARMA"),
        ("apply leave for harsh in sales department", "HARSHAL PATEL"),
        ("apply leave for harsh who is a manager", "HARSHAL PATEL"),
        ("apply leave for harsh from interns", "HARSHIT SHARMA"),
    ]
    for msg, want in cases:
        check("narrow", msg, _narrow_name(msg), want)


# --------------------------------------------------------------------------
# 6) Relative dates
# --------------------------------------------------------------------------
def test_relative_dates():
    section("relative dates")
    import datetime
    today = datetime.date.today()
    d = lambda off: (today + datetime.timedelta(days=off)).strftime("%Y-%m-%d")
    cases = [
        ("apply leave today", d(0)),
        ("apply leave tomorrow", d(1)),
        ("apply leave yesterday", d(-1)),
        ("apply leave day after tomorrow", d(2)),
        ("mere kl ki leave", d(1)),
        ("mere prso ki leave", d(2)),
        ("mere aaj ki leave", d(0)),
    ]
    for msg, want in cases:
        got = extract_relative_date(msg)
        # extract_relative_date returns {from_date, to_date, no_of_days}
        got_date = got.get("from_date") if isinstance(got, dict) else got
        check("date", msg, got_date, want)


# --------------------------------------------------------------------------
# 7) Content guard
# --------------------------------------------------------------------------
def test_apply_date_resolution():
    section("apply date resolution (weekday / natural date)")
    import datetime
    from app.services.leave_action_executor import extract_dates_from_message as ed
    today = datetime.date.today()

    def got(msg):
        r = ed(msg)
        return r.get("from_date") or "(none)"

    # explicit natural date
    check("applydate", "for 22 june 2026",
          got("apply leave for 22 june 2026"), "2026-06-22")
    # weekday -> upcoming occurrence (computed relative to today)
    def next_wd(target):
        days = (target - today.weekday()) % 7
        return (today + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    check("applydate", "for monday", got("apply leave for monday"), next_wd(0))
    check("applydate", "for friday", got("apply leave for friday"), next_wd(4))
    # a name must NOT be parsed as a date
    check("applydate", "for harsh (name, not date)",
          got("apply leave for harsh"), "(none)")
    # a weekday after "for" is a DATE, not an employee name
    from app.services.leave_action_executor import extract_employee_name_for_action as ena
    check("applydate", "wednesday not a name",
          ena("aplly leav for wednesday"), "")


def test_experience_filter():
    section("experience filter (fast path, no LLM)")
    from app.intent.fast_intent import parse_fast_intent as pfi

    def exp(msg):
        d = pfi(msg)
        if d is None:
            return "DEFER"
        got = {k: v for k, v in d["filters"].items()
               if k.startswith("experience") and v}
        return got or "NONE"

    check("exp", "more than 5 years",
          exp("who all have experience more than 5 years"), {"experience_gt": "5"})
    check("exp", "at least 3 years",
          exp("employees with at least 3 years experience"), {"experience_gte": "3"})
    check("exp", "less than 2 saal",
          exp("staff with less than 2 saal experience"), {"experience_lt": "2"})


def test_year_filter():
    section("year filter (leave history)")
    from app.intent.fast_intent import parse_fast_intent as pfi
    cases = [
        ("show piyush leaves of year 2026", ("2026-01-01", "2026-12-31")),
        ("harshal leaves in 2025", ("2025-01-01", "2025-12-31")),
        ("my leave history 2026", ("2026-01-01", "2026-12-31")),
    ]
    for msg, (wf, wt) in cases:
        d = pfi(msg) or {"filters": {}}
        f = d.get("filters", {})
        check("year", msg, (f.get("date_from"), f.get("date_to")), (wf, wt))


def test_content_guard():
    section("content guard")
    # must block
    for msg in ["show me your dick", "bdsm", "lond dikhado"]:
        check("guard", f"block: {msg}", is_inappropriate(msg), True)
    # must NOT block legit HR / common words
    for msg in ["sexual harassment complaint", "analysis of annual leave",
                "show employees in london", "class schedule"]:
        check("guard", f"allow: {msg}", is_inappropriate(msg), False)


# --------------------------------------------------------------------------
# 8) Attendance date window (future-guard, weekday clip, marked_by)
# --------------------------------------------------------------------------
def test_attendance_window():
    section("attendance date window")
    import datetime
    from app.services.attendance_window import resolve_attendance_window as rw
    TODAY = datetime.date(2026, 6, 17)  # a Wednesday, for deterministic results

    def win(msg):
        r = rw(msg, today=TODAY)
        if r.get("error_message"):
            return "FUTURE"
        s = r["date_from"] + ".." + r["date_to"]
        if r.get("marked_by"):
            s += "|" + r["marked_by"]
        return s

    cases = [
        ("show my attendance", "2026-06-17..2026-06-17"),
        ("attendance today", "2026-06-17..2026-06-17"),
        ("attendance yesterday", "2026-06-16..2026-06-16"),
        ("attendance tomorrow", "FUTURE"),
        ("attendance day after tomorrow", "FUTURE"),
        ("attendance monday", "2026-06-15..2026-06-15"),
        ("attendance friday", "FUTURE"),
        ("attendance monday to friday", "2026-06-15..2026-06-17"),  # clipped to today
        ("attendance this week", "2026-06-15..2026-06-17"),
        ("mere kl ki attendance", "FUTURE"),
        ("attendance on 15/06/2026", "2026-06-15..2026-06-15"),
        ("attendance 20 june", "FUTURE"),
        ("my office hours today", "2026-06-17..2026-06-17|Office Hours"),
        ("system hours yesterday", "2026-06-16..2026-06-16|System Hours"),
    ]
    for msg, want in cases:
        check("attendance", msg, win(msg), want)


def test_typo_tolerance():
    section("typo tolerance (domain words fixed, names safe)")
    fix = [
        ("aprove harshal leav", "approve harshal leave"),
        ("anual leav balnce", "annual leave balance"),
        ("show employes", "show employees"),
        ("rejct purav leaves", "reject purav leaves"),
        ("atendance of harshal", "attendance of harshal"),
        ("historry of leaves", "history of leaves"),
        ("departmnt wise", "department wise"),
    ]
    for raw, want in fix:
        check("typo", raw, normalize_typos(raw), want)
    for nm in ["harshal", "purav", "vikrant", "tanish", "mayank", "sahil",
               "rahul", "aditya", "neha", "anil", "sneha", "karan", "piyush",
               "shashank", "pooja"]:
        check("typo-name", nm, normalize_typos(nm), nm)


def _entity_of(q):
    d = parse_fast_intent(q)
    return d["entity"] if d else None


def _name_of(q):
    d = parse_fast_intent(q)
    return (d["filters"].get("employee_name") if d else None) or ""


def test_routing_and_names():
    section("routing + name extraction (varied phrasings)")
    check("route", "can purav take 10 annual leaves -> leave",
          _entity_of("can purav take 10 annual leaves"), "leave")
    check("route", "can purav take 10 days annual leave -> leave",
          _entity_of("can purav take 10 days annual leave"), "leave")
    check("route", "can i take 5 leaves -> leave",
          _entity_of("can i take 5 leaves"), "leave")
    check("route", "history of purav leaves -> leave_history",
          _entity_of("history of purav leaves"), "leave_history")
    check("route", "show mayank leave history -> leave_history",
          _entity_of("show mayank leave history"), "leave_history")
    check("route", "my balance -> leave", _entity_of("my leave balance"), "leave")
    check("name2", "history of purav leaves -> Purav",
          _name_of("history of purav leaves"), "Purav")
    check("name2", "can purav take 10 annual leaves -> Purav",
          _name_of("can purav take 10 annual leaves"), "Purav")
    check("name2", "which leave type harshal used most -> Harshal",
          _name_of("which leave type harshal have used the most"), "Harshal")
    check("name2", "harshal sick leave balance -> Harshal",
          _name_of("harshal sick leave balance"), "Harshal")
    check("name2", "show my leave history -> self (no name)",
          _name_of("show my leave history"), "")


def test_bulk_actions():
    section("bulk / multi-person actions")
    def items(q):
        r = parse_bulk_actions(q)
        return None if r is None else [(i["action"], i["name"], i["scope"]) for i in r]
    check("bulk", "3 comma actions",
          items("Approve vikrant, reject purav's, cancel harshal"),
          [("approve_leave", "vikrant", "all"), ("reject_leave", "purav", "all"),
           ("cancel_leave", "harshal", "all")])
    check("bulk", "2 names one action",
          items("approve harshal and tanish leaves"),
          [("approve_leave", "harshal", "all"), ("approve_leave", "tanish", "all")])
    check("bulk", "2 actions",
          items("approve harshal and reject purav leaves"),
          [("approve_leave", "harshal", "all"), ("reject_leave", "purav", "all")])
    check("bulk", "carry action across 'and'",
          items("reject purav and harshal"),
          [("reject_leave", "purav", "all"), ("reject_leave", "harshal", "all")])
    check("bulk", "single last leave -> None (picker)",
          items("approve last leave of harshal"), None)
    check("bulk", "multi with last scope",
          items("reject purav's last leave and cancel tanish leave"),
          [("reject_leave", "purav", "last"), ("cancel_leave", "tanish", "last")])
    check("bulk", "single plural -> None (picker)", items("approve harshal leaves"), None)
    check("bulk", "single + all -> bulk all",
          items("approve all harshal leaves"),
          [("approve_leave", "harshal", "all")])
    check("bulk", "single singular -> None", items("approve harshal leave"), None)
    check("bulk", "negation -> None", items("do not approve harshal"), None)
    check("bulk", "read -> None", items("show harshal leaves"), None)
    check("bulk", "apply -> None (single)", items("apply leave for harshal"), None)


def test_pagination():
    section("pagination decision + page size")
    for total, want in [(8, 8), (10, 10), (11, 6), (22, 11), (27, 14),
                        (50, 25), (70, 25)]:
        check("page", "page_size(" + str(total) + ")", _page_size(total), want)
    def kind(entity, target, n, msg):
        recs = [{"leave_type": "Sick", "from_date": "2026-06-01T00:00:00Z",
                 "to_date": "2026-06-01T00:00:00Z", "days": 1, "status": 1,
                 "employee_name": "X", "department": "P", "designation": "D",
                 "experience": 2}] * n
        out = _maybe_paginate_list({"original_message": msg, "filters": {}}, entity, target, recs)
        return None if out is None else _json.loads(out)["type"]
    check("page", "leave history -> list", kind("leave_history", "self", 12, "show my leave history"), "list")
    check("page", "employees -> list", kind("employee", "multiple", 12, "show all employees"), "list")
    check("page", "insight NOT paginated", kind("leave_history", "self", 12, "which leave type i used the most"), None)
    check("page", "count NOT paginated", kind("leave_history", "self", 12, "how many leaves in june"), None)
    check("page", "single profile NOT a list", kind("employee", "employee", 1, "show harshal profile"), None)


def test_smart_answers():
    section("smart answers (accurate numbers)")
    bal = [{"leave_type": "Annual Leave", "balance": 8.0},
           {"leave_type": "Sick Leave", "balance": 14.0},
           {"leave_type": "Comp Off", "balance": 2.0}]
    def ans(msg, who=""):
        return _smart_answer_raw(msg, {"entity": "leave", "filters": {"employee_name": who}}, bal)
    a10 = ans("can purav take 10 days annual leave", "Purav")
    check("smart", "can take 10 (have 8) -> No + 8", a10.startswith("No") and "8" in a10, True)
    check("smart", "can take 5 (have 8) -> Yes",
          ans("can purav take 5 days annual leave", "Purav").startswith("Yes"), True)
    check("smart", "if take 3 -> 5 left", "5" in ans("if i take 3 days annual leave"), True)
    check("smart", "annual balance line",
          ans("annual leave balance"), "You have 8 days of Annual Leave remaining.")
    hist = [{"leave_type": "Sick Leave", "from_date": "2026-01-05T00:00:00Z", "days": 2.0},
            {"leave_type": "Sick Leave", "from_date": "2026-03-05T00:00:00Z", "days": 3.0},
            {"leave_type": "Annual Leave", "from_date": "2026-02-05T00:00:00Z", "days": 1.0}]
    out = _smart_answer_raw("which leave type i have used the most",
                            {"entity": "leave_history", "filters": {"employee_name": ""}}, hist)
    check("smart", "most-used = Sick (5 days)", "Sick" in out and "5" in out, True)


def test_weird_queries():
    section("weird / messy queries route sanely")
    weird = [
        ("plz show me my leav balnce", "leave"),
        ("purav ka leave history dikhao", "leave_history"),
        ("employees in project dept", "employee"),
        ("how many sick leaves did i take", "leave_history"),
    ]
    for q, want in weird:
        check("weird", q, _entity_of(normalize_typos(q)), want)


def test_name_safety():
    section("name safety — junk words must NEVER become a person's name")
    valid = {"purav", "harshal", "vikrant", "tanish", "mayank", "sahil",
             "rahul", "aditya", "neha", "anil", "sneha", "karan", "piyush",
             "shashank", "pooja", "chandani", "john", "doe", "harsh",
             "rastogi", "saxena", "sharma", "patel", "chauhan", "rathi",
             "monday", "tuesday", "wednesday"}
    queries = [
        "could you tell me my leave balance", "please show my balance",
        "can you check purav balance", "would you mind showing harshal balance",
        "kindly show my leave balance", "may I know my balance",
        "I would like to see my leave history", "tell me about purav leaves",
        "give me a summary of my leaves", "I want to view harshal leave history",
        "is it possible for me to take 5 days", "do I have enough leave for 10 days",
        "will I be able to take 3 days annual", "am I allowed to take 7 days",
        "can harshal afford to take 4 leaves", "could purav take a week off",
        "I want to apply for sick leave tomorrow", "book annual leave for me next week",
        "go ahead and cancel tanish leave", "approve harshal and reject purav",
        "could you approve vikrant and harshal", "reject purav, cancel tanish and approve mayank",
        "who took the most leaves", "which employee has the highest leave",
        "employees who took maximum leave in january 2026", "show me the person with least leaves",
        "approve my leave", "reject my vacation request",
        "show me everyone in project", "list all staff",
        "employees with more than 5 years experience", "show me the maximum leave",
        "give me the latest leave", "show the pending requests", "show upcoming holidays",
        "what about my annual leave", "show me the total leaves", "show recent leaves",
        "mujhe meri balance dikhao", "purav ki history batao", "harshal ko approve kardo",
        "meri pending leaves dikhao", "sabhi employees dikhao", "project wale log dikhao",
    ]
    bad = []
    for q in queries:
        d = parse_fast_intent(q)
        nm = (d["filters"].get("employee_name") if d else "") or ""
        names = (d["filters"].get("employee_names") if d else []) or []
        for n in ([nm] if nm else []) + list(names):
            if any(tok not in valid for tok in n.lower().split()):
                bad.append((q, n))
                break
    check("name-safety", str(len(queries)) + " adversarial queries, 0 junk names",
          bad, [])


def main():
    print("HRBuddy eval suite — deterministic layer\n" + "=" * 44)
    test_entity_routing()
    test_fast_intent()
    test_should_use_llm()
    test_action_detection()
    test_name_extraction()
    test_code_extraction()
    test_disambiguation()
    test_relative_dates()
    test_content_guard()
    test_attendance_window()
    test_apply_date_resolution()
    test_experience_filter()
    test_year_filter()
    test_typo_tolerance()
    test_routing_and_names()
    test_bulk_actions()
    test_pagination()
    test_smart_answers()
    test_weird_queries()
    test_name_safety()

    total = _TOTAL["pass"] + _TOTAL["fail"]
    print("\n" + "=" * 44)
    print(f"RESULT: {_TOTAL['pass']}/{total} passed, {_TOTAL['fail']} failed")
    if _FAILURES:
        print("\nFAILURES:")
        for f in _FAILURES:
            print("  - " + f)
        return 1
    print("All good \u2705")
    return 0


if __name__ == "__main__":
    sys.exit(main())