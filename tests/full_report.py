# -*- coding: utf-8 -*-
"""FULL coverage over EVERY category in test_scenarios.py, using the real agent
ordering. Shows where each query routes (action / ranking / on-leave / read
entity / defer). Clean categories get a pass%; inherently-mixed ones get a
routing breakdown so nothing is hidden."""
import sys, types, re
_st = types.SimpleNamespace(chat=lambda **k: {"message": {"content": ""}})
_m = types.ModuleType("ollama"); _m.chat=lambda **k:{"message":{"content":""}}
_m.Client=lambda *a,**k:_st; sys.modules["ollama"]=_m; sys.path.insert(0,".")
from app.intent.fast_intent import parse_fast_intent
from app.agents.hr_agent import detect_action_intent, _name_low_confidence
from app.services.leave_action_executor import parse_bulk_actions
from app.services import comparison as C

ns={}; exec(open("tests/test_scenarios.py",encoding="utf-8").read(),ns)

def route(q):
    """Return (bucket, detail) mirroring process_message order."""
    if C.is_on_leave_query(q): return ("on_leave","")
    if C.is_comparison_query(q) and len(C.extract_comparison_names(q))>=2: return ("compare","")
    if C.is_org_ranking_query(q) and len(C.extract_comparison_names(q))<2: return ("ranking","")
    a=detect_action_intent(q)
    if a: return ("action",a)
    try:
        if parse_bulk_actions(q): return ("bulk","")
    except Exception: pass
    d=parse_fast_intent(q)
    if not d: return ("defer","")  # -> Ollama
    nm=(d.get("filters",{}) or {}).get("employee_name") or ""
    return ("read", f"{d['entity']}/{d['target']}"+(f"/name={nm}" if nm else ""))

# expected primary bucket for the clean categories
EXPECT = {
 "LEAVE_BALANCE_QUERIES":lambda b,d: b=="read" and d.startswith("leave/"),
 "LEAVE_HISTORY_QUERIES":lambda b,d: b=="read" and "leave_history" in d,
 "EMPLOYEE_PROFILE_QUERIES":lambda b,d: b=="read" and d.startswith("employee/"),
 "APPLY_LEAVE_QUERIES":lambda b,d: b in("action","bulk") and (d=="apply_leave" or b=="bulk"),
 "APPROVE_LEAVE_QUERIES":lambda b,d: b in("action","bulk"),
 "REJECT_LEAVE_QUERIES":lambda b,d: b in("action","bulk"),
 "CANCEL_LEAVE_QUERIES":lambda b,d: b in("action","bulk"),
 "EMPLOYEE_LIST_QUERIES":lambda b,d: b=="read" and "employee/multiple" in d,
 "EMPLOYEE_SEARCH_QUERIES":lambda b,d: b=="read" and d.startswith("employee/"),
 "DEPARTMENT_FILTER_QUERIES":lambda b,d: (b=="read" and "employee/multiple" in d) or b=="defer",
 "DESIGNATION_FILTER_QUERIES":lambda b,d: (b=="read" and "employee/multiple" in d) or b=="defer",
 "EXPERIENCE_FILTER_QUERIES":lambda b,d: (b=="read" and "employee/multiple" in d) or b=="ranking",
 "LEAVE_BALANCE_OTHER_EMPLOYEE_QUERIES":lambda b,d: b=="read" and d.startswith("leave/"),
 "LEAVE_HISTORY_FILTER_QUERIES":lambda b,d: b=="read" and "leave_history" in d,
 "MONTH_FILTER_QUERIES":lambda b,d: b=="read" and "leave" in d,
 "YEAR_FILTER_QUERIES":lambda b,d: b=="read" and "leave" in d,
 "DATE_FILTER_QUERIES":lambda b,d: b=="read" and "leave" in d,
 "LEAVE_TYPE_QUERIES":lambda b,d: b=="read" and "leave" in d,
 "SMART_QUESTIONS":lambda b,d: b in("read","defer") and ("leave" in d or b=="defer"),
 "COUNT_QUERIES":lambda b,d: b in("read","ranking") and ("leave" in d or b=="ranking"),
 "MAX_MIN_QUERIES":lambda b,d: b in("ranking","read"),
 "COMPARISON_QUERIES":lambda b,d: b in("compare","ranking","defer"),
 "HINGLISH_QUERIES":lambda b,d: b!="read" or "name=" not in d,
 "NEGATION_QUERIES":lambda b,d: b!="action",  # must NOT fire the action
 "ANALYTICS_QUERIES":lambda b,d: b in("ranking","read","defer","on_leave"),
}
# safety category: a data-read/action on jailbreak text is a concern
def jb_ok(b,d): return b in("defer",) or (b=="read" and "name=" not in d)

lists=[k for k in ns if k.endswith("_QUERIES") or k.endswith("_QUESTIONS")]
print("%-38s %-10s %s"%("CATEGORY","RESULT","note")); print("-"*70)
mixed=("MASTER_COMBO_QUERIES","MIXED_LANGUAGE_QUERIES","LONG_CONVERSATIONAL_QUERIES",
       "AMBIGUOUS_QUERIES","TYPO_QUERIES","ATTENDANCE_QUERIES","SUMMARY_QUERIES")
tot_ok=tot=0
for k in sorted(lists):
    qs=ns[k]
    if k=="JAILBREAK_AND_SECURITY_QUERIES":
        bad=[q for q in qs if not jb_ok(*route(q))]
        print("%-38s %-10s %s"%(k[:38], "%d/%d"%(len(qs)-len(bad),len(qs)),
              "queries that reach a data read/action (Ollama+guard must catch)"))
        continue
    if k in mixed:
        from collections import Counter
        c=Counter(route(q)[0] for q in qs)
        print("%-38s %-10s %s"%(k[:38],"n/a", dict(c)))
        continue
    ck=EXPECT.get(k)
    if not ck:
        print("%-38s %-10s"%(k[:38],"-")); continue
    ok=sum(1 for q in qs if ck(*route(q)))
    tot_ok+=ok; tot+=len(qs)
    print("%-38s %-10s"%(k[:38], "%d/%d %d%%"%(ok,len(qs),round(100*ok/len(qs)))))
print("-"*70)
print("SCOREABLE IN-SCOPE: %d/%d = %d%%"%(tot_ok,tot,round(100*tot_ok/tot)))