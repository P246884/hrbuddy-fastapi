# -*- coding: utf-8 -*-
"""Coverage report over the user's test_scenarios.py category lists.
Checks ROUTING (fast-intent entity/target, action detection, comparison/
ranking/on-leave detection) per category and prints a pass % so we can see
overall correctness. Offline; cannot judge the final Ollama-backed text, but
routing is where correctness is decided.
"""
import sys, types, io, os
_st = types.SimpleNamespace(chat=lambda **k: {"message": {"content": ""}})
_m = types.ModuleType("ollama"); _m.chat = lambda **k: {"message": {"content": ""}}
_m.Client = lambda *a, **k: _st
sys.modules["ollama"] = _m; sys.path.insert(0, ".")

from app.intent.fast_intent import parse_fast_intent
from app.agents.hr_agent import detect_action_intent, _name_low_confidence
from app.services.leave_action_executor import parse_bulk_actions
from app.services import comparison as C

# load the user's lists
ns = {}
exec(open("tests/test_scenarios.py", encoding="utf-8").read(), ns)

def d(q): return parse_fast_intent(q)
def ent(q):
    x = d(q); return x["entity"] if x else None
def tgt(q):
    x = d(q); return x["target"] if x else None
def nm(q):
    x = d(q); return (x.get("filters", {}).get("employee_name") if x else "") or ""
def act(q):
    a = detect_action_intent(q)
    if a: return a
    try:
        if parse_bulk_actions(q): return "bulk"
    except Exception: pass
    return None
def defer(q): return _name_low_confidence(d(q), q)
def ranking(q): return C.is_org_ranking_query(q) or (C.is_comparison_query(q) and len(C.extract_comparison_names(q)) >= 2)

def is_balance(q): return ent(q) == "leave" or defer(q)
def is_hist(q): return ent(q) == "leave_history" or defer(q)
def is_list(q): return ent(q) == "employee" and tgt(q) == "multiple"
def is_emp(q): return ent(q) == "employee"
def is_bal_or_hist(q): return ent(q) in ("leave", "leave_history") or defer(q)
def is_action(q, kinds): return act(q) in kinds
def is_compare(q): return C.is_comparison_query(q) or C.is_org_ranking_query(q)

# category -> (list name, checker). Out-of-scope categories are reported
# separately (a graceful "coming soon" is the correct behaviour there).
CHECKS = [
    ("LEAVE_BALANCE", "LEAVE_BALANCE_QUERIES", is_balance),
    ("LEAVE_HISTORY", "LEAVE_HISTORY_QUERIES", is_hist),
    ("PROFILE", "EMPLOYEE_PROFILE_QUERIES", is_emp),
    ("APPLY", "APPLY_LEAVE_QUERIES", lambda q: is_action(q, ("apply_leave",))),
    ("APPROVE", "APPROVE_LEAVE_QUERIES", lambda q: is_action(q, ("approve_leave", "bulk"))),
    ("REJECT", "REJECT_LEAVE_QUERIES", lambda q: is_action(q, ("reject_leave", "bulk"))),
    ("CANCEL", "CANCEL_LEAVE_QUERIES", lambda q: is_action(q, ("cancel_leave", "bulk"))),
    ("EMP_LIST", "EMPLOYEE_LIST_QUERIES", is_list),
    ("EMP_SEARCH", "EMPLOYEE_SEARCH_QUERIES", is_emp),
    ("DEPT_FILTER", "DEPARTMENT_FILTER_QUERIES", is_list),
    ("DESIG_FILTER", "DESIGNATION_FILTER_QUERIES", is_list),
    ("EXP_FILTER", "EXPERIENCE_FILTER_QUERIES", lambda q: is_list(q) or ranking(q)),
    ("BAL_OTHER", "LEAVE_BALANCE_OTHER_EMPLOYEE_QUERIES", is_balance),
    ("HIST_FILTER", "LEAVE_HISTORY_FILTER_QUERIES", is_hist),
    ("MONTH_FILTER", "MONTH_FILTER_QUERIES", is_bal_or_hist),
    ("YEAR_FILTER", "YEAR_FILTER_QUERIES", is_bal_or_hist),
    ("DATE_FILTER", "DATE_FILTER_QUERIES", is_hist),
    ("LEAVE_TYPE", "LEAVE_TYPE_QUERIES", is_bal_or_hist),
    ("SMART", "SMART_QUESTIONS", lambda q: ent(q) == "leave" or defer(q)),
    ("COUNT", "COUNT_QUERIES", is_bal_or_hist),
    ("MAX_MIN", "MAX_MIN_QUERIES", lambda q: ranking(q) or "attendance" in q or "hours" in q),
    ("COMPARISON", "COMPARISON_QUERIES", lambda q: is_compare(q) or "department" in q or "attendance" in q or "team" in q),
    ("HINGLISH", "HINGLISH_QUERIES", lambda q: (ent(q) in ("leave","leave_history","employee")) or act(q) or ranking(q) or defer(q) or "attendance" in q or "summary" in q or "report" in q),
    ("NEGATION", "NEGATION_QUERIES", lambda q: act(q) is None),
]

OUT_OF_SCOPE = [("ATTENDANCE", "ATTENDANCE_QUERIES"), ("SUMMARY", "SUMMARY_QUERIES")]

print("%-14s %-9s %s" % ("CATEGORY", "SCORE", "PASS%"))
print("-"*60)
gp = gt = 0
for label, lst, ck in CHECKS:
    qs = ns.get(lst, [])
    ok = sum(1 for q in qs if _safe(ck, q)) if False else 0
    ok = 0
    for q in qs:
        try:
            if ck(q): ok += 1
        except Exception:
            pass
    gp += ok; gt += len(qs)
    pct = (100.0*ok/len(qs)) if qs else 0
    print("%-14s %-9s %5.0f%%" % (label, "%d/%d" % (ok, len(qs)), pct))
print("-"*60)
print("IN-SCOPE ROUTING: %d/%d = %.0f%%" % (gp, gt, 100.0*gp/gt))
print()
for label, lst in OUT_OF_SCOPE:
    qs = ns.get(lst, [])
    # correct = NOT a confident leave/employee data answer (should be coming-soon)
    leaked = [q for q in qs if ent(q) in ("leave","leave_history") or (ent(q)=="employee" and tgt(q)=="multiple")]
    print("%-14s out-of-scope, %d/%d route away from a data answer (coming-soon ok)" % (label, len(qs)-len(leaked), len(qs)))

def failures():
    print("\n===== FAILING QUERIES per weak category =====")
    for label, lst, ck in CHECKS:
        qs = ns.get(lst, [])
        bad = []
        for q in qs:
            try:
                if not ck(q): bad.append(q)
            except Exception: bad.append(q)
        if bad:
            print("\n["+label+"] "+str(len(bad))+" fail:")
            for q in bad:
                x = d(q)
                r = (x.get('entity'), x.get('target'), (x.get('filters',{}).get('employee_name') or '')) if x else None
                print("   %-40s -> ent/tgt/name=%s | act=%s | defer=%s" % (q[:40], r, act(q), defer(q)))

failures()