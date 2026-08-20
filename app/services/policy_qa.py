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
    """'what policies do you have', 'list all policies', 'apar policies',
    'company policies', bare 'policies'. About the SET, not one policy."""
    m = (message or "").lower()
    if re.search(r"\b(apply|cancel|balance)\b", m):
        return False
    has_plural = bool(re.search(r"\b(policies|documents)\b", m))
    if not has_plural:
        return False
    # a specific policy named with the singular word "policy" is NOT a list
    is_specific = bool(re.search(r"\b(notice|leave|wfh|travel|dress|attendance|"
                                 r"maternity|paternity|probation|reimbursement|"
                                 r"whistle|bribery|grievance|disciplinary|"
                                 r"referral|separation|diversity|pip|health)\s+"
                                 r"policy\b", m))
    if is_specific:
        return False
    # plural "policies" with a list/scope verb OR on its own (e.g. "apar
    # policies", "company policies", "policies") -> list them all.
    has_list_verb = bool(re.search(r"\b(list|all|which|what|show|available|"
                                   r"our|apar|company|org|organization|"
                                   r"every|the)\b", m))
    # bare "policies?" (very short) also counts as a list request
    word_count = len(re.findall(r"[a-z]{3,}", m))
    return has_list_verb or word_count <= 2


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


def _is_personal_or_action(m):
    """True if the message is a leave-action or personal-data query (NOT policy)
    unless it explicitly says policy/rule. These belong to other handlers."""
    if re.search(r"\b(policy|policies|rule|rules|guideline|handbook|sop|procedure)\b", m):
        return False
    if re.search(r"\b(apply|cancel|approve|reject|pending|compare|balance|"
                 r"history|profile|manager|on leave|who is|whos|my leave|"
                 r"leave today|leave status|salary|holiday|birthday|experience|"
                 r"designation|department|team|employees?|staff|report|"
                 r"count|how many|no leave|zero leave|0 leave|taken|took|"
                 r"this month|last month|calendar)\b", m):
        return True
    # a date reference ("1 january", "on 28 aug", "2026-01-01") -> not policy
    if re.search(r"\b\d{1,2}\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                 r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}"
                 r"|\b20\d{2}-\d{2}-\d{2}\b", m):
        return True
    if re.search(r"\bmy\b", m):
        return True
    return False


def _all_policy_topics():
    try:
        auto = policy_rag.policy_topic_words()
    except Exception:
        auto = set()
    return set(_POLICY_TOPICS) | auto


def is_explicit_policy_query(message):
    """EARLY routing: fires only when the query clearly names a policy (word
    'policy'/'rule'/'handbook', or a known multi-word policy phrase like
    'whistle blower'). Safe to run before leave-action handlers."""
    m = (message or "").lower()
    if _is_personal_or_action(m):
        return False
    if re.search(r"\b(policy|policies|rule|rules|guideline|handbook|sop|procedure)\b", m):
        return True
    multiword = [t for t in _all_policy_topics() if " " in t]
    if any(t in m for t in multiword):
        return True
    if re.search(r"\b(download|export|send|share)\b", m) and \
       re.search(r"\b(policy|policies|document|pdf|handbook)\b", m):
        return True
    return False


def is_policy_content_query(message):
    """LATE fallback (after every structured handler failed): the question may
    be answerable from a policy PDF (e.g. 'rewards for reference'). Requires a
    genuine content match so it never answers random chit-chat."""
    m = (message or "").lower()
    if _is_personal_or_action(m):
        return False
    if len(re.findall(r"[a-z]{3,}", m)) < 2:
        return False
    try:
        return policy_rag.query_matches_policies(message)
    except Exception:
        return False


def is_policy_query(message):
    return is_explicit_policy_query(message) or is_policy_content_query(message)


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