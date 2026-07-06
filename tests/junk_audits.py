# -*- coding: utf-8 -*-
"""Audit for junk names using the REAL agent ordering: action / ranking /
on-leave get first crack (they ignore the read-path name), so only queries
that actually reach the READ path can break on a bogus name."""
import sys, types, re
_st = types.SimpleNamespace(chat=lambda **k: {"message": {"content": ""}})
_m = types.ModuleType("ollama"); _m.chat = lambda **k: {"message": {"content": ""}}
_m.Client = lambda *a, **k: _st
sys.modules["ollama"] = _m; sys.path.insert(0, ".")
from app.intent.fast_intent import parse_fast_intent
from app.agents.hr_agent import detect_action_intent, _name_low_confidence
from app.services.leave_action_executor import parse_bulk_actions
from app.services import comparison as C

ns = {}
exec(open("tests/test_scenarios.py", encoding="utf-8").read(), ns)

def intercepted_before_read(q):
    # mirrors process_message order before STEP-3 read
    if C.is_on_leave_query(q): return "on_leave"
    if C.is_comparison_query(q) and len(C.extract_comparison_names(q)) >= 2: return "compare"
    if C.is_org_ranking_query(q) and len(C.extract_comparison_names(q)) < 2: return "ranking"
    if detect_action_intent(q): return "action"
    try:
        if parse_bulk_actions(q): return "bulk"
    except Exception: pass
    return None

def name_ctx(q):
    return bool(re.search(r"\b(of|for)\s+[a-z]|[a-z]'s\b|\bki\b|\bka\b|employee\s+[a-z0-9]|"
                          r"\b(harshal|purav|vikrant|tanish|mayank|sahil|rahul|aditya|neha|pooja|harsh)\b", q, re.I))

lists = [k for k in ns if k.endswith("_QUERIES") or k.endswith("_QUESTIONS")]
real_junk = []
for lk in lists:
    if lk.startswith(("ATTENDANCE","SUMMARY","JAILBREAK")):  # out of scope / security
        continue
    for q in ns[lk]:
        if intercepted_before_read(q):
            continue  # handled by action/ranking/on-leave; read-path name unused
        d = parse_fast_intent(q)
        if not d: continue
        nm = (d.get("filters", {}) or {}).get("employee_name") or ""
        if nm and not name_ctx(q):
            real_junk.append((lk.replace("_QUERIES","").replace("_QUESTIONS",""), q, nm, d.get("entity")))

print("TRUE read-path junk-name breakages:", len(real_junk))
print("="*66)
cur=None
for cat,q,nm,ent in real_junk:
    if cat!=cur: print("\n["+cat+"]"); cur=cat
    print("   %-40s name='%s' ent=%s" % (q[:40], nm, ent))