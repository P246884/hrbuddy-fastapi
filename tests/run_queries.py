"""
HRBuddy LIVE query runner — fire lots of messy/varied queries at the REAL
running service and capture every response so you can eyeball them in one go.

This is the end-to-end companion to eval_suite.py:
  - eval_suite.py  -> offline, rule layer only (no Ollama/CRM), CI-friendly.
  - run_queries.py -> hits the live /chat (Ollama + CRM + formatting), so you
                      see the ACTUAL answer a user would get.

------------------------------------------------------------------------------
HOW TO USE
------------------------------------------------------------------------------
1. Start the service (uvicorn) as usual.
2. Get a valid bearer TOKEN (the same JWT the web app uses). Then either:
     - set an env var:   set HRBUDDY_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJVc2VyR3VpZCI6IjFkMzRmMmQ0LTE4MmItZjAxMS04YzRlLTAwMjI0OGQ2Njg5OCIsIlVzZXJOYW1lIjoiUFJBVEhBTSAgU0hBUk1BIiwiQnVzaW5lc3NHdWlkIjoiOTQ1NmI0ZmUtMzMyNy1mMDExLThjNGQtMDAwZDNhZjJhNDczIiwiSXNIUiI6IlRydWUiLCJJc0FkbWluIjoiVHJ1ZSIsIkVtcGxveWVlVHlwZSI6Ilx1MDAwMFx1MDAwMFx1MDAwMFx1MDAwMyIsImV4cCI6MTc4MjM5NTM3NCwiaXNzIjoiSFJCdWRkeSIsImF1ZCI6IkhSQnVkZHlDbGllbnQifQ.oHIRJrDvVQVMmVl9BCEm4E3qj5kURHUa-BEHZR5NtEg     (Windows)
                         export HRBUDDY_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJVc2VyR3VpZCI6IjFkMzRmMmQ0LTE4MmItZjAxMS04YzRlLTAwMjI0OGQ2Njg5OCIsIlVzZXJOYW1lIjoiUFJBVEhBTSAgU0hBUk1BIiwiQnVzaW5lc3NHdWlkIjoiOTQ1NmI0ZmUtMzMyNy1mMDExLThjNGQtMDAwZDNhZjJhNDczIiwiSXNIUiI6IlRydWUiLCJJc0FkbWluIjoiVHJ1ZSIsIkVtcGxveWVlVHlwZSI6Ilx1MDAwMFx1MDAwMFx1MDAwMFx1MDAwMyIsImV4cCI6MTc4MjM5NTM3NCwiaXNzIjoiSFJCdWRkeSIsImF1ZCI6IkhSQnVkZHlDbGllbnQifQ.oHIRJrDvVQVMmVl9BCEm4E3qj5kURHUa-BEHZR5NtEg
                            (Mac/Linux)
     - OR paste it into TOKEN below.
3. Check BASE_URL points to your server.
4. Run from the project root:
       python tests\\run_queries.py
5. Read the console, and the full transcript saved to:
       tests\\run_queries_output.txt

By default ONLY read-only queries run. Action queries (apply/approve/reject/
cancel/bulk) MODIFY real CRM data, so they are OFF. Flip RUN_ACTIONS = True
only against a test account/sandbox when you explicitly want to test them.
------------------------------------------------------------------------------
"""

import os
import json
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("HRBUDDY_URL", "http://localhost:8001")
TOKEN = os.environ.get("HRBUDDY_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJVc2VyR3VpZCI6IjFkMzRmMmQ0LTE4MmItZjAxMS04YzRlLTAwMjI0OGQ2Njg5OCIsIlVzZXJOYW1lIjoiUFJBVEhBTSAgU0hBUk1BIiwiQnVzaW5lc3NHdWlkIjoiOTQ1NmI0ZmUtMzMyNy1mMDExLThjNGQtMDAwZDNhZjJhNDczIiwiSXNIUiI6IlRydWUiLCJJc0FkbWluIjoiVHJ1ZSIsIkVtcGxveWVlVHlwZSI6Ilx1MDAwMFx1MDAwMFx1MDAwMFx1MDAwMyIsImV4cCI6MTc4MjM5NTM3NCwiaXNzIjoiSFJCdWRkeSIsImF1ZCI6IkhSQnVkZHlDbGllbnQifQ.oHIRJrDvVQVMmVl9BCEm4E3qj5kURHUa-BEHZR5NtEg")
RUN_ACTIONS = False          # True = also run mutating actions (CHANGES DATA!)
PAUSE_SECONDS = 0.3          # small gap so you don't hammer Ollama
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "run_queries_output.txt")


# ---------------------------------------------------------------------------
# QUERY SETS  (add your own freely — one string per line)
# ---------------------------------------------------------------------------
READ_QUERIES = {
    "balance (self)": [
        "my leave balance",
        "show my leave balance",
        "what's my annual leave balance",
        "anual leav balnce",                       # typos
        "sick leave balance kitni hai",
        "comp off balance",
        "how many carry forward left",
        "mera annual leave kitna bacha hai",        # hinglish
    ],
    "feasibility / projection (self)": [
        "can i take 5 days annual leave",
        "can i take 20 days annual leave",
        "if i take 3 days annual leave what will my balance be",
        "am i close to exhausting my balance",
        "can i take 2 leaves next month",
    ],
    "leave history (self)": [
        "show my leave history",
        "my leaves",
        "how many leaves did i take in january",
        "how many sick leaves did i take",
        "total leaves i took this year",
        "which leave type have i used the most",
        "which month do i take most leaves",
        "what is my average monthly leave usage",
        "show my approved leaves",
        "my rejected leaves",
        "my cancelled leaves",
        "my pending leaves",
        "leaves of year 2025",
    ],
    "other people (needs HR/manager rights)": [
        "history of purav leaves",
        "show mayank leave history",
        "purav sick leave balance",
        "which leave type harshal have used the most",
        "can purav take 10 days annual leave",
        "can purav take 10 annual leaves",
        "who is manager of purav",
        "purav's manager",
    ],
    "employees": [
        "show all employees",
        "list employees",
        "show employees",
        "employees in project department",
        "employees in project dept",
        "show employees department wise",
        "employees whose name starts with a",
        "employees whose name related to harsh",
        "who all have experience more than 5 years",
        "show employes",                           # typo
    ],
    "self / misc": [
        "what is my name",
        "who am i",
        "tell me my name",
    ],
    "weird / hinglish / messy": [
        "yaar mera leave kitna bacha",
        "purav ka leave history dikhao",
        "plz show me my leav balnce",
        "kitni chutti bची hai",
        "show me everything about my leaves",
        "leaves????",
        "    ",                                     # blank-ish
        "asdkjfh random text",                      # garbage -> should degrade gracefully
    ],
    "out of scope (should politely decline / coming soon)": [
        "tell me a joke",
        "what is the weather today",
        "write me python code",
        "what is my salary",
        "book a meeting room",
    ],
}

# These CHANGE DATA. Only run with RUN_ACTIONS=True on a test account.
ACTION_QUERIES = {
    "single actions": [
        "apply sick leave for tomorrow",
        "approve harshal leave",
        "do not apply leave",                       # negation -> should NOT act
    ],
    "bulk actions": [
        "approve harshal and tanish leaves",
        "approve vikrant, reject purav's, cancel harshal leaves",
        "reject purav and harshal",
        "approve last leave of harshal",
        "aprove vikrant, rejct purav, cancl harshal leavs",   # typo bulk
    ],
}


# ---------------------------------------------------------------------------
# RUNNER
# ---------------------------------------------------------------------------
def ask(message):
    """POST one message to /chat and return a short readable response string."""
    try:
        r = requests.post(
            BASE_URL.rstrip("/") + "/chat",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + TOKEN},
            json={"message": message},
            verify=False,
            timeout=120,
        )
    except Exception as ex:
        return "ERROR: request failed -> " + str(ex)

    if r.status_code in (401, 403):
        return "UNAUTHORISED (" + str(r.status_code) + ") — token missing/expired."
    if r.status_code != 200:
        return "HTTP " + str(r.status_code) + " — " + r.text[:200]

    raw = (r.text or "").replace("\x1fLIVE\x1f", "").strip()
    # try to interpret a structured JSON response
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("type"):
            t = data["type"]
            if t == "list":
                head = data.get("intro") or data.get("title") or (str(data.get("count")) + " records")
                items = data.get("items", [])[:3]
                sample = " | ".join(
                    (it.get("primary", "") + ((" [" + it.get("badge", "") + "]") if it.get("badge") else ""))
                    for it in items
                )
                return "[list] " + head + "  ::  " + sample + (" ..." if data.get("count", 0) > 3 else "")
            return "[" + t + "] " + str(data.get("message", "")).replace("\n", " ")
    except Exception:
        pass
    # plain text (streamed) answer
    return raw.replace("\n", " ")[:500] if raw else "(empty response)"


def run(sets, header, out):
    line = "\n" + "=" * 70 + "\n" + header + "\n" + "=" * 70
    print(line); out.append(line)
    total = 0
    for category, queries in sets.items():
        cat = "\n--- " + category + " ---"
        print(cat); out.append(cat)
        for q in queries:
            total += 1
            ans = ask(q)
            row = "▶ " + q + "\n   → " + ans
            print(row); out.append(row)
            time.sleep(PAUSE_SECONDS)
    return total


def main():
    if TOKEN == "PASTE_YOUR_TOKEN_HERE":
        print("⚠  Set HRBUDDY_TOKEN env var or edit TOKEN in this file first.")
        return 1

    out = []
    n = run(READ_QUERIES, "READ-ONLY QUERIES (safe)", out)

    if RUN_ACTIONS:
        n += run(ACTION_QUERIES, "ACTION QUERIES (⚠ MODIFIES DATA)", out)
    else:
        skip = ("\n(Action queries skipped — set RUN_ACTIONS=True to include them, "
                "ideally on a test account.)")
        print(skip); out.append(skip)

    footer = "\n" + "=" * 70 + "\nDONE — " + str(n) + " queries sent. Review above / " + OUTPUT_FILE
    print(footer); out.append(footer)

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
    except Exception as ex:
        print("Could not write output file:", ex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())