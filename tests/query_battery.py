import sys, types
_st = types.SimpleNamespace(chat=lambda **k: {"message": {"content": ""}})
_m = types.ModuleType("ollama"); _m.chat = lambda **k: {"message": {"content": ""}}
_m.Client = lambda *a, **k: _st
sys.modules["ollama"] = _m; sys.path.insert(0, ".")
from app.intent.fast_intent import parse_fast_intent
from app.agents.hr_agent import detect_action_intent, _name_low_confidence
from app.services.leave_action_executor import parse_bulk_actions
from app.services import comparison as C

def _dec(q): return parse_fast_intent(q)
def ent(q):
    d=_dec(q); return d["entity"] if d else None
def tgt(q):
    d=_dec(q); return d["target"] if d else None
def name_of(q):
    d=_dec(q); return (d.get("filters",{}).get("employee_name") if d else "") or ""
def act(q):
    a=detect_action_intent(q)
    if a: return a
    try:
        if parse_bulk_actions(q): return "bulk"
    except Exception: pass
    return None
def defers(q):
    return _name_low_confidence(_dec(q), q)

def is_balance(q): return ent(q)=="leave" or defers(q)
def is_history(q): return ent(q)=="leave_history"
def is_list(q): return ent(q)=="employee" and tgt(q)=="multiple"
def is_profile(q): return ent(q)=="employee"
def no_junk(q):
    # PASS if no name extracted, or a junk name is deferred to Ollama
    return (not name_of(q)) or defers(q)

CATS=[]
def cat(n,qs,ck): CATS.append((n,qs,ck))

cat("BALANCE",["show my leave balance","remaining leaves","leave balance","how many leaves do i have","how many leaves are left","how many leaves do i have left","how many annual leaves do i have","available leaves","what is my remaining leave balance","how much leave is available","leave left?","remaining leave?","meri kitni leave bachi hai","kitni leave baki hai","kitni leaves left hain","how many leaves are available","do i still have any leaves left","leaves left","check my available leaves","how many sick leaves are left"], is_balance)
cat("HISTORY",["show my leave history","my leave history","leave history","show previous leaves","show all my leaves","what leaves have i taken","show approved leaves","show pending leaves","my leave records","display my previous leaves","past leave history","list my leaves"], is_history)
cat("EMP_LIST",["show all employees","list all employees","display all employees","show every employee","show everyone","display every employee","give me all employee records","show everyone in the company","all employees","show staff","sab employees dikhao","employee directory"], is_list)
cat("PROFILE",["show my profile","my profile","who am i","my details","show harshal profile","who is harshal","show details of harshal","what is my experience","my department","who is my manager"], is_profile)
cat("APPLY",["apply leave","apply sick leave","apply leave tomorrow","book my leave","i need leave today","apply annual leave","leave apply karo"], lambda q: act(q)=="apply_leave")
cat("APPROVE",["approve leave","approve harshal leave","approve harshal's leave","please approve harshal leave","harshal ki leave approve kar do","go ahead and approve harshal leave","approve pending leave"], lambda q: act(q) in ("approve_leave","bulk"))
cat("REJECT",["reject leave","reject harshal leave","please reject harshal leave","decline harshal leave request","purav ki leave reject kar do"], lambda q: act(q) in ("reject_leave","bulk"))
cat("CANCEL",["cancel my leave","withdraw my leave","cancel harshal leave","meri leave cancel kar do","cancel my latest leave"], lambda q: act(q) in ("cancel_leave","bulk"))
cat("NEGATION",["don't approve harshal leave","do not approve the leave","don't cancel my leave","never approve pending leave","don't book leave"], lambda q: act(q) is None)
cat("COMPARE",["compare purav and harshal leaves","purav vs harshal","compare purav, harshal and vikrant this year","who took more leaves purav or harshal","compare experience of purav and tanish"], lambda q: C.is_comparison_query(q) and len(C.extract_comparison_names(q))>=2)
cat("ORG_RANK",["who took the most leaves","who took the least leaves","who took the most leaves in january","most experienced employee","sabse jyada experience kiska hai","top 3 employees by leave","who has the highest experience"], lambda q: C.is_org_ranking_query(q))
cat("ON_LEAVE",["who is on leave","who is on leave today","employees on leave in january","who all were on leave this week","whos absent today","show me employees who are on leave in january"], lambda q: C.is_on_leave_query(q))
cat("RANDOM_NOJUNK",["yo how many leaves i got left bro","umm can u tell me my leaves pls","leave balance???","i wanna know how many days off i have","do i have leaves or not","kitni chutti bachi hai bhai","mujhe meri baki leave batao","how many holidays remaining for me","am i out of leaves","whats left in my leave account","leaveeee balanceee","how many leave i take this year","gimme my leave info","i think i have some leaves left how many","tell me if i can take a day off"], no_junk)

tot=0
print("%-14s %-7s %s"%("CATEGORY","SCORE","FAILURES")); print("-"*72)
for n,qs,ck in CATS:
    bad=[]
    for q in qs:
        try: ok=ck(q)
        except Exception as e: ok=False; q=q+" <ERR:"+str(e)[:25]+">"
        if not ok: bad.append(q)
    tot+=len(bad)
    print("%-14s %-7s%s"%(n,"%d/%d"%(len(qs)-len(bad),len(qs)),"  OK" if not bad else "  FAIL: "+" | ".join(bad)))
print("-"*72); print("TOTAL FAILURES:",tot)