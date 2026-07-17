import re
from app.intent.fast_intent import (
    parse_fast_intent,
    clean_text
)
from app.crm.entity_registry import (
    ENTITY_REGISTRY,
    STATUS_MAPPING,
    ACTION_PATTERNS
)
from app.llm.ollama_client import (
    extract_decision,
    chat_response
)
from app.services.dynamic_executor import execute
from app.services.leave_action_executor import execute_leave_action
from app.tools.translator import (
    translate_to_english,
    normalize_numbers
)


def should_translate(text):
    try:
        text.encode("ascii")
        return False
    except:
        return True


def looks_like_hr_query(message: str):
    msg = clean_text(message)
    hr_terms = set()

    for entity_name, config in ENTITY_REGISTRY.items():
        hr_terms.add(clean_text(entity_name))
        for alias in config.get("aliases", []):
            hr_terms.add(clean_text(alias))
        for leave_type in config.get("allowed_types", []):
            hr_terms.add(clean_text(leave_type))

    action_words = [
        "leave", "chutti", "bimar", "apply",
        "approve", "reject", "cancel",
        # Out-of-scope HR topics — these should also be caught
        "salary", "payroll", "payslip", "attendance",
        "reimbursement", "claim", "pf", "esi",
        "tax", "tds", "appraisal", "performance",
        "training", "asset", "resignation",
        "overtime", "shift", "document"
    ]
    hr_terms.update(action_words)

    return any(term in msg for term in hr_terms)


def _action_tokens(msg):
    return [t for t in re.split(r"[^a-z0-9]+", msg) if t]


def _fuzzy_any(tokens, targets, cutoff=0.80):
    """True if any token exactly equals or is a close typo of a target word."""
    import difflib
    for t in tokens:
        if t in targets:
            return True
        if len(t) >= 3 and difflib.get_close_matches(t, targets, n=1, cutoff=cutoff):
            return True
    return False


def detect_action_intent(message: str):
    msg = clean_text(message)
    tokens = _action_tokens(msg)

    # Question guard: "who is approving / who approves / who will approve my
    # leave", "who is my approver/manager" is a QUESTION about the approver, not
    # a command to approve/apply. Let the read path answer it (-> manager).
    if re.search(r"\b(who|whom|kaun|kon|kisne|kis)\b", msg) and \
       re.search(r"\bapprov|\bapprover\b|\breject|\brejecter\b|\bmanager\b|\breporting\b", msg):
        return None

    # Negation guard: "do not apply leave", "don't apply", "I don't want to
    # apply", "do no apply" -> NOT an action (user is explicitly refusing).
    if re.search(r"\b(do not|don'?\s?t|do no|donot|do nt|never|kindly do not|"
                 r"please don'?\s?t|please do not)\b(?:\s+\w+){0,3}\s+(apply|"
                 r"approve|reject|cancel|book|booking|revoke|delete|remove|"
                 r"withdraw|appl|aply|apli|apprve|aprove|rejct|"
                 r"cancl|cancle|lagao|laga|lgao|krdo|kardo)", msg):
        return None

    # Read / status guards first: "show ...", "... approved leaves" describe
    # records to FETCH, not actions to perform. These also stop a typo like
    # "applied" (status) from being mistaken for the verb "apply".
    is_read_query = bool(re.search(
        r"\b(show|list|display|view|get|fetch|dikhao|dikha|batao|kitni|kitne|"
        r"history|balance|remaining)\b", msg
    ))
    has_status_word = bool(re.search(
        r"\b(approved|rejected|cancelled|canceled|applied)\b",
        msg
    ))

    # "mark <X> leave as approved / rejected", "set ... to approved" is an
    # ACTION even though it contains a status word. Catch it before the status
    # guard blocks it.
    if re.search(r"\b(mark|set|change)\b", msg) and not is_read_query:
        if re.search(r"\b(as\s+)?approv", msg):
            return "approve_leave"
        if re.search(r"\b(as\s+)?reject", msg):
            return "reject_leave"
        if re.search(r"\b(as\s+)?cancel", msg):
            return "cancel_leave"

    # Explicit command phrasing that carries a status word but is really an
    # ACTION: "... should be rejected/approved", "... reject it" (with an
    # explicit reject/approve verb present) is a decision, not a history read.
    if not is_read_query:
        if re.search(r"should\s+be\s+approv|please\s+approv|\bapprov\w*\s+it\b", msg):
            return "approve_leave"
        if re.search(r"should\s+be\s+reject|please\s+reject|\breject\s+it\b|reject\s+instead", msg):
            return "reject_leave"
        if re.search(r"should\s+be\s+cancel|please\s+cancel|\bcancel\s+it\b", msg):
            return "cancel_leave"

    # "approval de do / approval chahiye" — an approval request even without the
    # word "leave" (leave is implied in this HR bot).
    if re.search(r"\bapproval\b", msg) and not is_read_query:
        return "approve_leave"

    # Leave context = a leave noun OR a leave TYPE — both fuzzy, so "appl sick
    # leave", "apply sik", "leave laga do" all register. Users mistype; we still
    # understand. Anything fuzzy can't catch falls through to Ollama (STEP 6),
    # which reads intent even from messy phrasing.
    LEAVE_NOUNS = ["leave", "leaves", "chutti", "chhutti", "chutee", "avkash",
                   "vacation", "vacations", "holiday", "holidays", "timeoff",
                   "time off", "day off", "days off", "leef", "off"]
    LEAVE_TYPES = ["sick", "annual", "casual", "comp", "compoff", "carry",
                   "privilege", "earned", "maternity", "paternity"]
    has_leave = (
        any(n in msg for n in LEAVE_NOUNS)
        or _fuzzy_any(tokens, LEAVE_NOUNS)
        or _fuzzy_any(tokens, LEAVE_TYPES)
        # "apply half day tomorrow afternoon" implies a leave in this HR bot
        or bool(re.search(r"\bhalf[\s-]?day\b|\baadh[ai]\b", msg))
    )

    # Cancel-intent phrases that must NOT be read as apply: "hata do/hatao"
    # (= remove), "don't need … anymore", "no longer need", "nahi chahiye".
    cancel_phrase = bool(re.search(
        r"\bhata\s*do\b|\bhatao\b|\bhatado\b|\bhata\b|no longer need|"
        r"don'?t need|nahi chahiye|nhi chahiye|not needed anymore|anymore",
        msg)) and has_leave
    if cancel_phrase and not is_read_query and not has_status_word:
        return "cancel_leave"

    # Hinglish / multi-word apply phrasings (substring) + fuzzy English verb.
    APPLY_MULTI = (
        "laga do", "lagado", "laga", "lgao", "lagao", "le lu", "le loon",
        "leni", "krdo", "kardo", "kar do", "krni", "karni", "chahiye", "chaiye",
        "want", "need", "bhej do", "bhejo", "bhej", "request", "raise",
    )
    has_apply = (_fuzzy_any(tokens, ["apply", "applying", "book", "booking",
                                     "create", "raise", "submit", "mark"])
                 or any(v in msg for v in APPLY_MULTI))
    other_action = _fuzzy_any(tokens, ["approve", "accept", "grant", "reject",
                                       "decline", "deny", "cancel", "withdraw",
                                       "revoke", "delete", "remove"]) or cancel_phrase

    # apply wins over the read/balance path, but never for read/status queries.
    if has_apply and has_leave and not other_action and not is_read_query and not has_status_word:
        return "apply_leave"

    # bare "half day tomorrow afternoon" (no explicit verb) still means apply a
    # half-day leave in this HR bot, as long as it isn't a read/status query.
    if (re.search(r"\bhalf[\s-]?day\b|\baadh[ai]\b", msg)
            and re.search(r"\btomorrow\b|\btoday\b|\byesterday\b|\bmorning\b|"
                          r"\bafternoon\b|\bevening\b|\b\d{1,2}\b|\bmonday\b|"
                          r"\btuesday\b|\bwednesday\b|\bthursday\b|\bfriday\b|"
                          r"\bsaturday\b|\bsunday\b", msg)
            and not other_action and not is_read_query and not has_status_word):
        return "apply_leave"

    # approve / reject / cancel — fuzzy verb + leave context. Only a TERMINAL
    # status word (approved/rejected/cancelled) blocks these — "applied"
    # /"requested"/"pending" merely describe a live leave that CAN be actioned
    # (e.g. "withdraw the leave I applied yesterday" -> cancel).
    terminal_status = bool(re.search(
        r"\b(approved|rejected|cancelled|canceled)\b", msg))
    if has_leave and not is_read_query and not terminal_status:
        if _fuzzy_any(tokens, ["approve", "accept", "grant"]):
            return "approve_leave"
        if _fuzzy_any(tokens, ["reject", "decline", "deny"]):
            return "reject_leave"
        if _fuzzy_any(tokens, ["cancel", "withdraw", "revoke", "delete", "remove"]):
            return "cancel_leave"

    for action, patterns in ACTION_PATTERNS.items():
        for pattern in patterns:
            if pattern in msg:
                return action
    return None


def _operation_to_action(operation: str):
    mapping = {
        "apply":   "apply_leave",
        "approve": "approve_leave",
        "reject":  "reject_leave",
        "cancel":  "cancel_leave",
    }
    return mapping.get(operation)


OUT_OF_SCOPE = {
    "salary": "💰 Salary details",
    "payroll": "💰 Payroll",
    "payslip": "💰 Payslip",
    "reimbursement": "🧾 Reimbursement",
    "claim": "🧾 Claims",
    "pf": "🏦 PF / Provident Fund",
    "esi": "🏦 ESI",
    "tax": "📄 Tax details",
    "tds": "📄 TDS",
    "appraisal": "⭐ Appraisal",
    "performance": "⭐ Performance",
    "training": "📚 Training",
    "asset": "💻 Assets",
    "resignation": "🚪 Resignation",
    "notice period": "🚪 Notice Period",
    "overtime": "⏰ Overtime",
    "shift": "🔄 Shift",
    "document": "📁 Documents",
    "attendance": "🕒 Attendance",
    "office hours": "🕒 Office Hours",
    "system hours": "🕒 System Hours",
    "working hours": "🕒 Working Hours",
    "dashboard": "📊 Dashboard",
}


_YES_WORDS = {
    "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "okk", "confirm",
    "confirmed", "proceed", "go", "haan", "ha", "han", "hanji", "haanji",
    "theek", "thik", "kardo", "kar", "krdo", "karo", "bilkul", "yess", "yo",
    "ji", "jee", "done", "ya",
}
_NO_WORDS = {
    "no", "n", "nope", "nah", "naa", "na", "nahi", "mat", "cancel", "stop",
    "abort", "chodo", "rehne", "nai", "dont", "never", "rukja", "ruko",
}


def _interpret_yes_no(reply):
    """Classify a free-text reply to a confirmation as 'yes' / 'no' / 'unknown'."""
    import re as _re
    r = _re.sub(r"[^a-z\s]", " ", (reply or "").lower())
    r = _re.sub(r"\s+", " ", r).strip()
    if not r:
        return "unknown"
    toks = set(r.split())
    phrases_yes = ("go ahead", "do it", "kar do", "theek hai", "thik hai",
                   "yes please", "haan ji")
    phrases_no = ("do not", "rehne do", "not now", "leave it", "mat karo")
    if any(p in r for p in phrases_no):
        return "no"
    if any(p in r for p in phrases_yes):
        return "yes"
    has_yes = bool(toks & _YES_WORDS)
    has_no = bool(toks & _NO_WORDS)
    if has_no and not has_yes:
        return "no"
    if has_yes and not has_no:
        return "yes"
    return "unknown"


def _name_low_confidence(decision, original_message):
    """A first-person query ('how many leaves do I have left', 'bachi hain
    meri') should never resolve to a person's name pulled from a leftover
    lowercase word ('Left', 'Hain'). If a name was extracted in a clearly
    first-person sentence and that name never appears as a real (capitalised)
    proper noun in the original text, treat the decision as low-confidence and
    hand it to Ollama to infer the true intent."""
    if not decision:
        return False
    filt = decision.get("filters") or {}
    nm = (filt.get("employee_name") or "").strip()
    if not nm or filt.get("employee_code"):
        return False
    if not _re_conf.search(r"\b(i|my|me|mine|myself|meri|mera|mere|meri|mujhe|"
                           r"maine|hume|humko)\b", original_message or "", _re_conf.I):
        return False
    # keep it only if a name token shows up capitalised (a real proper noun)
    for tok in nm.split():
        if _re_conf.search(r"\b" + _re_conf.escape(tok) + r"\b", original_message or ""):
            return False
    return True


import re as _re_conf


def _coming_soon():
    return (
        "🚧 That's coming soon!\n\n"
        "Right now I can help you with:\n"
        "• 👤 Employees — profiles & directory\n"
        "• 📊 Leave balances\n"
        "• 📋 Leave history & insights\n"
        "• ✅ Leave apply / approve / reject / cancel (single or bulk)\n\n"
        "More modules will be available shortly! 🚀"
    )


def process_message(
    message: str,
    user: dict,
    token: str,
    pending_context: dict = None
):
    if should_translate(message):
        translated_message = translate_to_english(message)
    else:
        translated_message = message

    translated_message = normalize_numbers(translated_message)

    # Fix typos in DOMAIN words (actions/leave types/entities/status) so the
    # whole pipeline understands "aprove anual leav" as "approve annual leave".
    # Names are never altered.
    from app.intent.fast_intent import normalize_typos
    translated_message = normalize_typos(translated_message)

    print("ORIGINAL:", message)
    print("TRANSLATED:", translated_message)

    # ----------------------------------
    # STEP 0: CONTENT GUARD
    # Vulgar / explicit input gets a fixed professional redirect and never
    # reaches the LLM. (Legitimate HR terms like "sexual harassment" are
    # intentionally NOT blocked.)
    # ----------------------------------
    from app.security.content_guard import is_inappropriate, safe_redirect_message
    if is_inappropriate(message) or is_inappropriate(translated_message):
        return safe_redirect_message(), None

    # ----------------------------------
    # STEP 0b: HOW-TO / GUIDANCE questions are help, not data lookups.
    # "how do I approve a leave request" must NOT dump the user's leaves.
    # ----------------------------------
    import re as _re0
    _ml = translated_message.lower()
    if (_re0.search(r"\bhow (do|to|can|would|should) i\b", _ml)
            or _re0.search(r"\bhow to\b", _ml)
            or _re0.search(r"\b(guide|help) me (with|on|to)\b", _ml)
            or _re0.search(r"what is the (procedure|process|step)", _ml)):
        return (
            "Here's how I work — just tell me what you need in plain language:\n"
            "• 📊 \"What's my leave balance?\"\n"
            "• 📋 \"Show my leave history\" / \"how many leaves did I take in June\"\n"
            "• ✅ \"Approve harshal's leave\" or bulk: \"approve vikrant, reject purav, cancel harshal leaves\"\n"
            "• 📝 \"Apply sick leave for tomorrow\"\n"
            "• 👤 \"Show employees in Project\" / \"who is manager of purav\"\n\n"
            "Go ahead and ask directly — I'll take it from there."
        ), None

    # ----------------------------------
    # STEP 0b-2: CHANGING / MODIFYING an existing leave's dates is not supported
    # (there's no edit endpoint). Tell the user to cancel and re-apply instead.
    # ----------------------------------
    if (_re0.search(r"\b(change|chang|modif|edit|reschedul|re-?schedul|"
                    r"postpone|postpon|prepone|prepon|move|shift|alter|amend|"
                    r"extend|shorten|swap|badal|badl)\w*",
                    _ml)
            and _re0.search(r"\b(leave|leaves|chutti|vacation|holiday|"
                            r"time off)\b", _ml)
            and not _re0.search(r"\b(status|balance|history|show|list|kitni|"
                                r"kitne)\b", _ml)):
        import random as _rnd0
        return (_rnd0.choice([
            "I can't change the dates of an existing leave directly. Please "
            "cancel that leave and apply a fresh one with the new dates.",
            "Editing a leave's dates isn't supported — the way to do it is to "
            "cancel the current leave and apply again for the new dates.",
            "Leave dates can't be modified once applied. Just cancel it and "
            "raise a new leave for the date you want.",
            "There's no direct way to move a leave to another date. Cancel the "
            "existing one, then apply a new leave for the new date.",
        ]) + " For example: \"cancel my leave for 13th July\", then \"apply "
             "annual leave for 14th July\"."), None

    # ----------------------------------
    # STEP 0c: COMPARISON / RANKING between named people
    # "compare purav and harshal leaves this year", "who took the most leaves
    # between X and Y", "compare experience of A and B". Needs >=2 names; an
    # all-employee ranking (no names) is still coming soon.
    # ----------------------------------
    # ----------------------------------
    # STEP 0e: cross-employee "who is on leave" — scan the org for the date
    # window (default today) and list who has approved leave.
    # ----------------------------------
    from app.services.comparison import (is_comparison_query, build_comparison,
                                          extract_comparison_names,
                                          is_org_ranking_query, build_org_ranking,
                                          is_on_leave_query, build_on_leave,
                                          is_org_pending_query, build_org_pending,
                                          is_dept_leave_query, build_dept_leave,
                                          is_group_balance_query, build_group_balance,
                                          is_low_balance_query, build_low_balance)
    if is_on_leave_query(translated_message):
        return build_on_leave(translated_message, user, token), None

    # Low balance ("who has less than N days left") — a filtered balance view.
    # Checked before the full group-balance so a threshold query doesn't dump
    # everyone's balance.
    if is_low_balance_query(translated_message):
        return build_low_balance(translated_message, user, token), None

    # Group leave BALANCE: "leave balance for project department",
    # "balance for designation Team Member". Beats the generic dept-leave view.
    if is_group_balance_query(translated_message):
        return build_group_balance(translated_message, user, token), None

    # Org-wide / team pending requests: "show all pending leaves across the org",
    # "pending leaves in finance department". Checked before the generic dept
    # view because a pending+department query should still land here.
    if is_org_pending_query(translated_message):
        return build_org_pending(translated_message, user, token), None

    # Department leave data (any status): "show leave data for finance department".
    if is_dept_leave_query(translated_message):
        return build_dept_leave(translated_message, user, token), None

    # STEP 0e-2: holidays (company list) and birthdays (from employee DOB).
    from app.services.celebrations import (is_holiday_query, build_holidays,
                                           is_birthday_query, build_birthdays)
    if is_birthday_query(translated_message):
        return build_birthdays(translated_message, user, token), None
    if is_holiday_query(translated_message):
        return build_holidays(translated_message, user, token), None

    if is_comparison_query(translated_message):
        if len(extract_comparison_names(translated_message)) >= 2:
            return build_comparison(translated_message, user, token), None
        if is_org_ranking_query(translated_message):
            return build_org_ranking(translated_message, user, token), None
    elif is_org_ranking_query(translated_message) \
            and len(extract_comparison_names(translated_message)) < 2:
        return build_org_ranking(translated_message, user, token), None

    # ----------------------------------
    # STEP 0d: you can't approve/reject your OWN leave — that's your manager's
    # job. (Cancelling your own leave IS allowed and falls through normally.)
    # ----------------------------------
    if (_re0.search(r"\b(approve|reject)\b", _ml)
            and _re0.search(r"\bmy\b", _ml)
            and _re0.search(r"\b(leave|leaves|vacation|request|holiday|time off)\b", _ml)
            # "who will approve my leave" is a QUESTION about the approver, not a
            # request to self-approve -> let it fall through to the manager answer.
            and not _re0.search(r"\b(who|whom|kaun|kon|kis|kisko)\b", _ml)):
        import random as _rnd
        return (_rnd.choice([
            "You can't approve or reject your own leave — that's your manager's "
            "call. Want me to show your pending requests, or apply a new leave?",
            "Approving your own leave isn't allowed — your manager handles that. "
            "I can pull up your pending leaves or file a new one if you like.",
            "That one's up to your manager, not you — self-approval isn't allowed. "
            "I can show what's pending or apply a fresh leave for you.",
        ])), None

    # ----------------------------------
    # STEP 1: PENDING CONFIRMATION (yes / no / haan / naa)
    # If we previously asked the user to confirm something, their reply decides
    # it — it is NOT treated as a brand-new command. Anything that isn't a
    # clear yes/no falls through and is handled as a fresh message.
    # ----------------------------------
    from app.services.leave_action_executor import (
        parse_bulk_actions, handle_bulk_action, confirm_bulk_response,
        _execute_leave_action_by_guid, _confirm_single_response
    )
    if pending_context and pending_context.get("_confirm"):
        verdict = _interpret_yes_no(translated_message)
        if verdict == "yes":
            if pending_context["_confirm"] == "bulk":
                return handle_bulk_action(
                    pending_context.get("items", []),
                    pending_context.get("message", ""), user, token
                )
            if pending_context["_confirm"] == "single":
                return _execute_leave_action_by_guid(
                    pending_context.get("action"),
                    pending_context.get("leave_guid", ""),
                    translated_message, user, token
                )
        elif verdict == "no":
            return "Okay, cancelled — nothing was changed. 👍", None
        else:
            # Not a clear yes/no. If the reply is itself a NEW command, let it
            # through; otherwise stay in the confirmation and ask again.
            _is_new_cmd = bool(
                parse_bulk_actions(translated_message)
                or detect_action_intent(translated_message)
            )
            if not _is_new_cmd:
                _d = parse_fast_intent(translated_message)
                _is_new_cmd = bool(_d and _d.get("entity") in
                                   ("leave", "leave_history", "employee"))
            if not _is_new_cmd and pending_context["_confirm"] == "bulk":
                return confirm_bulk_response(
                    pending_context.get("items", []),
                    pending_context.get("message", ""),
                    prefix="Please reply yes or no. "
                ), None
            if not _is_new_cmd and pending_context["_confirm"] == "single":
                # keep it simple on re-ask — reuse the stored context as-is
                import json as _json
                return _json.dumps({
                    "type": "confirm",
                    "message": "Please reply yes or no — should I go ahead?",
                    "context": pending_context,
                }), None
            # else: fall through and handle as a fresh message

    # ----------------------------------
    # STEP 1b: PENDING ACTION (picker flows)
    # ----------------------------------
    if pending_context and pending_context.get("action"):
        return execute_leave_action(
            action=pending_context["action"],
            message=translated_message,
            user=user,
            token=token,
            pending_context=pending_context
        )

    # ----------------------------------
    # STEP 2: FAST ACTION INTENT
    # ----------------------------------
    # 2a) BULK / multi-person actions: confirm first, then run on "yes".
    bulk_items = parse_bulk_actions(translated_message)
    if bulk_items:
        print("BULK ACTIONS (await confirm):", bulk_items)
        return confirm_bulk_response(bulk_items, translated_message), None

    action = detect_action_intent(translated_message)
    if action:
        print("ACTION DETECTED:", action)
        return execute_leave_action(
            action=action,
            message=translated_message,
            user=user,
            token=token,
            pending_context=None
        )

    # ----------------------------------
    # STEP 3: FAST INTENT (read queries)
    # ----------------------------------
    decision = parse_fast_intent(translated_message)

    # If the fast path guessed a person's name in a first-person sentence with
    # no real proper noun, it's almost certainly a junk name ("Left", "Hain").
    # Don't answer confidently wrong — let Ollama infer the real intent below.
    if _name_low_confidence(decision, message) or _name_low_confidence(decision, translated_message):
        print("LOW-CONFIDENCE NAME -> deferring to Ollama:", decision)
        decision = None

    if decision and decision.get("entity") == "smalltalk":
        return decision["answer"], None

    if decision:
        decision["original_message"] = message
        print("FAST DECISION:", decision)

        if decision.get("entity") == "analytics":
            return decision.get("answer"), None

        # Scope is currently Leave + Employees only. Anything else (e.g.
        # attendance) is acknowledged as coming soon rather than half-answered.
        if decision.get("entity") not in ("leave", "leave_history", "employee"):
            return _coming_soon(), None

        return execute(
            decision=decision,
            user=user,
            token=token
        ), None

    # ----------------------------------
    # STEP 4: HR query check
    # ----------------------------------
    if not looks_like_hr_query(translated_message):
        return chat_response(translated_message), None

    # ----------------------------------
    # STEP 5: OUT OF SCOPE CHECK
    # Known HR topics but not yet implemented
    # ----------------------------------
    import re as _re
    msg_lower = translated_message.lower()
    for keyword, label in OUT_OF_SCOPE.items():
        # Whole word match — "esi" should not match "designation" or "intern"
        if _re.search(r'\b' + _re.escape(keyword) + r'\b', msg_lower):
            return (
                "🚧 **" + label + "** module is coming soon!\n\n"
                "I currently support:\n"
                "• 👤 Employee profiles & directory\n"
                "• 📊 Leave balances\n"
                "• 📋 Leave history\n"
                "• ✅ Leave apply / approve / reject / cancel\n"
                "• 📍 Attendance (in/out times, by date)\n\n"
                "More modules will be available shortly! 🚀"
            ), None

    # ----------------------------------
    # STEP 6: OLLAMA
    # ----------------------------------
    print("CALLING OLLAMA...")
    decision = extract_decision(translated_message)

    if decision:
        decision["original_message"] = message
        print("OLLAMA DECISION:", decision)

    # Ollama ne action return kiya
    if decision and decision.get("operation"):
        action_from_ollama = _operation_to_action(decision["operation"])
        if action_from_ollama:
            print("OLLAMA ACTION:", action_from_ollama)
            filters = decision.get("filters", {})
            ctx = {k: v for k, v in {
                "leave_type_name": filters.get("type", ""),
                "from_date": filters.get("from_date", ""),
                "to_date": filters.get("to_date", ""),
            }.items() if v}
            return execute_leave_action(
                action=action_from_ollama,
                message=translated_message,
                user=user,
                token=token,
                pending_context=ctx if ctx else None
            )

    # Ollama ne read entity return kiya
    if decision and decision.get("entity"):
        if decision.get("entity") not in ("leave", "leave_history", "employee"):
            return _coming_soon(), None
        return execute(
            decision=decision,
            user=user,
            token=token
        ), None

    # ----------------------------------
    # STEP 7: GENERAL CHAT
    # ----------------------------------
    return chat_response(translated_message), None