"""
Policy question detection + response building.

Bridges the chat flow to the policy RAG engine: decides whether a message is a
policy question, produces a chat response with the answer, and attaches the
source policy PDF (base64) so the frontend can offer a download button.
"""

import re
import os
import json
import base64

from app.rag import policy_rag


# words that signal the user is asking about a company policy / document
_POLICY_WORDS = (
    "policy", "policies", "rule", "rules", "guideline", "guidelines",
    "handbook", "sop", "procedure", "eligibility", "entitlement",
    "code of conduct", "leave policy", "wfh policy", "travel policy",
    "reimbursement", "notice period", "probation", "gratuity", "pf",
    "provident fund", "maternity", "paternity", "harassment", "posh",
    "dress code", "attendance policy", "holiday policy", "benefits",
)

# question framing that, combined with a document, is a policy lookup
_ASK_WORDS = ("what", "how", "when", "can i", "am i", "is there", "explain",
              "tell me", "does", "do we", "whats", "what's", "which", "why",
              "eligible", "allowed", "entitled", "rule", "according")


def is_policy_list_query(message):
    """'what policies do you have', 'list all policies', 'show policy documents'.
    Must be about the SET of policies, not a specific policy question."""
    m = (message or "").lower()
    if re.search(r"\b(apply|cancel|balance)\b", m):
        return False
    # needs an explicit list verb AND the plural 'policies'/'documents'
    has_list_verb = bool(re.search(r"\b(list|all|which|what|show|available)\b", m))
    has_plural = bool(re.search(r"\b(policies|documents)\b", m))
    # "notice period policy" is singular+specific -> NOT a list query
    is_specific = bool(re.search(r"\b(notice|leave|wfh|travel|dress|attendance|"
                                 r"maternity|paternity|probation|reimbursement)\s+"
                                 r"policy\b", m))
    return has_list_verb and has_plural and not is_specific


# topic phrases that are policy questions even without the word "policy"
_POLICY_TOPICS = (
    "notice period", "annual leave", "sick leave", "casual leave",
    "work from home", "wfh", "remote work", "carry forward", "probation",
    "gratuity", "provident fund", "maternity", "paternity", "reimbursement",
    "dress code", "code of conduct", "harassment", "posh", "notice",
    "entitled to", "eligible for", "how many leaves", "how many days",
    # --- topics matching the org's actual policy documents ---
    "anti bribery", "bribery", "corruption", "travel", "disciplinary",
    "referral", "employee referral", "performance management", "pms",
    "appraisal", "separation", "resignation", "exit", "handbook",
    "diversity", "inclusion", "labour", "business ethics", "ethics",
    "grievance", "redressal", "escalation", "pip", "performance improvement",
    "whistle blower", "whistleblower", "workplace health", "health and safety",
    "safety", "conduct",
    # common conduct/ABC question phrases that don't say "policy"
    "gift", "gifts", "vendor", "vendors", "bribe", "bribes", "kickback",
    "hospitality", "conflict of interest", "gratuity", "favour", "favor",
    "accept a gift", "give a gift", "facilitation payment", "donation",
    "can i accept", "can i give", "am i allowed",
    # expense / reimbursement question words (live inside travel/expense docs)
    "expense", "expenses", "claim", "claims", "allowance", "per diem",
    "reimburse", "travel expense", "travel expenses", "daily allowance",
    "lodging", "boarding", "airfare", "mileage",
)


def is_policy_query(message):
    """True when the message looks like a question about a company policy.
    Kept conservative so it doesn't steal leave-action / balance queries."""
    m = (message or "").lower()

    # explicit action / personal-data queries are NOT policy questions
    if re.search(r"\b(apply|cancel|approve|reject)\b", m):
        if not re.search(r"\b(policy|policies|rule|rules|guideline|handbook|"
                         r"sop|procedure)\b", m):
            return False
    # leave-status / roster queries ("who is on leave", "pending leaves",
    # "leave balance/history") are handled by their own flows, not policy —
    # unless the user explicitly says "policy"/"rule".
    if re.search(r"\b(on leave|who is|whos|pending|balance|history|"
                 r"my leave|leave today|leave status)\b", m):
        if not re.search(r"\b(policy|policies|rule|rules)\b", m):
            return False
    # "my leave balance", "my leave history" -> personal data, not policy
    if re.search(r"\bmy\b", m) and re.search(r"\b(balance|history|leaves?|"
                                             r"profile)\b", m):
        if not re.search(r"\b(policy|policies|rule|rules)\b", m):
            return False

    has_policy_word = any(w in m for w in _POLICY_WORDS)
    if has_policy_word:
        return True

    # topic phrase + a question/entitlement framing.
    # Topics come from TWO sources:
    #   1. the hardcoded _POLICY_TOPICS list (common HR topics), and
    #   2. words auto-derived from the actual policy PDF filenames — so any
    #      new/renamed policy is recognised WITHOUT a code change.
    try:
        auto_topics = policy_rag.policy_topic_words()
    except Exception:
        auto_topics = set()
    all_topics = set(_POLICY_TOPICS) | auto_topics

    has_topic = any(t in m for t in all_topics)
    has_ask = bool(re.search(r"\b(what|how|when|can|am|is|are|do|does|which|"
                             r"why|explain|tell|eligible|allowed|entitled|"
                             r"many|get|apply for)\b", m))
    if has_topic and has_ask:
        return True

    # A full policy PHRASE (a two-word topic like "workplace health" or
    # "whistle blower") named on its own is a policy lookup even without a
    # question word — it clearly refers to a specific policy.
    multiword_topics = [t for t in all_topics if " " in t]
    if any(t in m for t in multiword_topics):
        return True

    # "download/export/show the <something> policy/document"
    if re.search(r"\b(download|export|send|share|show|give)\b", m) and \
       re.search(r"\b(policy|policies|document|pdf|handbook)\b", m):
        return True

    # CONTENT FALLBACK — the keyword lists can't name every term inside 15 PDFs
    # ("rewards for reference", "per-diem", "escalation matrix"...). So if the
    # question isn't a personal-data/action query and it STRONGLY matches the
    # policy documents themselves, treat it as a policy question. This makes any
    # topic that actually lives in a PDF answerable without a code change.
    has_ask_word = bool(re.search(r"\b(what|how|when|can|am|is|are|do|does|"
                                  r"which|why|explain|tell|list|reward|rewards|"
                                  r"amount|eligible|allowed|entitled|many|much|"
                                  r"get|process|procedure|rule|rules|steps?|"
                                  r"matrix|level|criteria|limit|eligibility|"
                                  r"details?|define|meaning|scope)\b", m))
    # also allow a bare 2+ word phrase (no question word) to be content-checked,
    # e.g. "escalation matrix", "rewards for reference" — a noun phrase that may
    # name a policy section.
    word_count = len(re.findall(r"[a-z]{3,}", m))
    if has_ask_word or word_count >= 2:
        try:
            if policy_rag.query_matches_policies(message):
                return True
        except Exception:
            pass
    return False


def _pdf_base64(path):
    try:
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
    except Exception:
        return None


def build_policy_list():
    policies = policy_rag.list_policies()
    if not policies:
        return json.dumps({
            "type": "policy_list",
            "intro": ("No policy documents are set up yet. Please ask HR to add "
                      "the policy PDFs."),
            "policies": [],
        })
    items = []
    for title, fname in policies:
        entry = {"title": title, "filename": fname}
        if policy_rag.policy_path(fname):
            entry["download_file"] = fname   # frontend uses /policy/download
        items.append(entry)
    return json.dumps({
        "type": "policy_list",
        "intro": "Here are all the policy documents I can help with:",
        "hint": "Ask me anything, e.g. \"what's the notice period policy?\"",
        "policies": items,
    })


def build_policy_answer_stream(message):
    """Streaming generator for a policy answer. Yields text pieces. The FIRST
    line is a special marker with the download info as JSON:
        __POLICY_META__{"attachment": {...}, "title": "..."}\n
    ...followed by the answer text streamed word-by-word. The frontend strips
    the marker line, renders the download button, and appends streamed text."""
    meta_sent = False
    saw_answer = False
    for item in policy_rag.answer_policy_question_stream(message):
        if isinstance(item, dict):
            if item.get("no_docs"):
                yield ("No policy documents are set up yet. Please ask HR to add "
                       "the policy PDFs so I can answer policy questions.")
                return
            if item.get("low_conf"):
                policies = policy_rag.list_policies()
                hint = ""
                if policies:
                    hint = ("\n\nPolicies I have: "
                            + ", ".join(t for t, _ in policies) + ".")
                yield ("I couldn't find that in the policy documents. Try "
                       "rephrasing, or name the policy." + hint)
                return
            # metadata dict -> build a SMALL marker (filename only, NOT the
            # base64 PDF — that huge blob must never travel through the answer
            # stream). The frontend downloads the PDF from /policy/download.
            fname = item.get("policy_file")
            title = item.get("policy_title") or "Policy"
            has_pdf = bool(policy_rag.policy_path(fname)) if fname else False
            marker = {"source_title": title,
                      "download_file": fname if has_pdf else None}
            yield "__POLICY_META__" + json.dumps(marker) + "\n"
            meta_sent = True
        else:
            # a piece of answer text
            saw_answer = True
            yield item
    if not saw_answer and meta_sent:
        yield "I couldn't generate an answer. Please try rephrasing."


def build_policy_answer(message):
    """Answer a policy question and attach the source PDF for download."""
    res = policy_rag.answer_policy_question(message)

    if res.get("no_docs"):
        return json.dumps({
            "type": "policy_answer",
            "answer": ("No policy documents are set up yet. Please ask HR to add "
                       "the policy PDFs so I can answer policy questions."),
            "attachment": None,
        })

    if res.get("low_conf") or not res.get("answer"):
        # offer the list so the user can pick
        policies = policy_rag.list_policies()
        hint = ""
        if policies:
            hint = ("\n\nPolicies I have: "
                    + ", ".join(t for t, _ in policies) + ".")
        return json.dumps({
            "type": "policy_answer",
            "answer": ("I couldn't find that in the policy documents. Try "
                       "rephrasing, or name the policy." + hint),
            "attachment": None,
        })

    answer = res["answer"]
    fname = res.get("policy_file")
    title = res.get("policy_title") or "Policy"

    attachment = None
    path = policy_rag.policy_path(fname) if fname else None
    if path:
        b64 = _pdf_base64(path)
        if b64:
            attachment = {
                "filename": fname,
                "title": title,
                "mimetype": "application/pdf",
                "documentbody": b64,
            }

    return json.dumps({
        "type": "policy_answer",
        "answer": answer,
        "source_title": title,
        "attachment": attachment,
    })