import re
import random
import difflib
import calendar
from datetime import date, timedelta
from dateutil import parser

from app.crm.entity_registry import ENTITY_REGISTRY
from app.intent.dynamic_filter_extractor import (
    extract_dynamic_filters
)

SMALL_TALK_PATTERNS = {
    "greeting": [
        "hi", "hello", "hey", "hii", "helo", "yo", "sup",
        "good morning", "good afternoon", "good evening"
    ],
    "how_are_you": [
        "how are you", "how r u", "hru", "how you doing",
        "how's it going", "whats up", "what's up", "wassup"
    ],
    "thanks": [
        "thanks", "thank you", "thx", "ty", "thanks buddy",
        "cool", "working", "cool working"
    ],
    "bye": [
        "bye", "goodbye", "see you", "cya", "take care"
    ]
}


COMMAND_WORDS = [
    "show", "get", "fetch", "give", "tell", "please",
    "send", "provide", "can", "you",
    "me", "my", "mine", "your", "yours",
    "of", "for", "the", "a", "an", "about",
    "details", "detail", "basic", "profile", "info",
    "information", "employee", "user", "staff",
    "record", "records",
    "balance", "balances", "leave", "leaves",
    "history", "request", "requests",
    "previous", "past", "approved", "rejected",
    "cancelled", "canceled", "requested",
    "last", "recent", "days", "day", "months", "month",
    "only", "exclude", "except", "without",
    "but", "dont", "don't", "don", "t", "not",
    "between", "from", "to",
    "compare", "versus", "vs", "share", "display",
    "ki", "ka", "ke", "ko", "se", "mein", "hai",
    "dikhao", "dikho", "batao", "bato", "do", "dedo",
    "or", "aur", "and"
]


COMPLEX_PATTERNS = [
    "who have taken",
    "summary",
    "trend",
    "analytics",
    "department wise",
    "designation wise",
    "compare",
    "versus",
    " vs ",
    "manager",
    "reports to",
    "reporting to",
]


def clean_text(text: str):
    text = text.lower().strip()
    text = text.replace("\u2019", "'")
    text = re.sub(r"([a-z])'s\b", r"\1", text)
    text = re.sub(r"[^a-z0-9\s/-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # normalise common "leave" misspellings so entity routing still works
    text = re.sub(r"\b(laeve|leaev|levae|lvae|laev|leav|leve)\b", "leave", text)
    return text


# ---------------------------------------------------------------------------
# TYPO TOLERANCE
# A small controlled vocabulary the corrector may snap a misspelt word to.
# Person NAMES are deliberately NOT in here, so a name can never be "corrected"
# into one of these words. Only domain words (actions / entities / leave types /
# status / commands / filters) are eligible. Anything the corrector can't place
# confidently is left exactly as typed (and Ollama handles the rest).
# ---------------------------------------------------------------------------
_VOCAB = {
    "approve", "reject", "cancel", "apply",
    "leave", "leaves", "balance", "balances", "remaining", "history",
    "employee", "employees", "staff", "attendance", "profile", "directory",
    "details", "detail",
    "sick", "annual", "casual", "earned", "privilege", "comp", "carry",
    "forward", "maternity", "paternity", "bereavement", "compensatory",
    "pending", "requested", "approved", "rejected", "cancelled", "applied",
    "show", "list", "display", "fetch", "provide", "search",
    "manager", "department", "designation", "experience", "name",
    "most", "average", "total", "count", "month", "today", "tomorrow",
    "yesterday",
    # valid words that must NOT be auto-corrected into a similar domain word
    # (e.g. "request" -> "requested" used to break cancel/approve detection)
    "request", "requests", "vacation", "vacations", "holiday", "holidays",
    "week", "weeks", "year", "years", "current", "next", "last", "this",
    "info", "information", "about",
}
# very common short typos (< 4 chars) that fuzzy matching is too risky for
_SHORT_TYPO_MAP = {
    "sik": "sick", "anu": "annual", "emp": "employee", "emps": "employees",
    "att": "attendance", "bal": "balance", "hist": "history",
    "dept": "department", "dept": "department", "desig": "designation",
}


def _correct_token(low):
    """Return the canonical vocab word for a (lowercased) token if it is an
    obvious misspelling of one; otherwise return the token unchanged."""
    if low in _VOCAB:
        return low
    if low in _SHORT_TYPO_MAP:
        return _SHORT_TYPO_MAP[low]
    if len(low) < 4 or not low.isalpha():
        return low
    match = difflib.get_close_matches(low, _VOCAB, n=1, cutoff=0.82)
    if match:
        return match[0]
    return low


def normalize_typos(message: str):
    """Fix typos in DOMAIN words while leaving names and everything else intact.
    Runs once at the start of request handling so all downstream routing,
    action detection and bulk parsing see corrected vocabulary."""
    if not message:
        return message
    out = []
    for tok in re.findall(r"\S+", message):
        m = re.match(r"^([A-Za-z]+)(.*)$", tok)
        if not m:
            out.append(tok)
            continue
        word, tail = m.group(1), m.group(2)
        fixed = _correct_token(word.lower())
        out.append((fixed + tail) if fixed != word.lower() else tok)
    return " ".join(out)


def should_use_llm(msg: str):
    msg = clean_text(msg)
    if any(pattern in msg for pattern in COMPLEX_PATTERNS):
        return True
    # NOTE: experience queries are no longer deferred — extract_experience()
    # handles "more than/less than/at least N years" on the fast path.
    if (
        ("designation" in msg or "department" in msg or "dept" in msg)
        and re.search(r"\b(or|ya)\b|,", msg)
    ):
        return True
    return False


def detect_small_talk(message: str):
    msg = clean_text(message)
    for category, phrases in SMALL_TALK_PATTERNS.items():
        if msg in phrases:
            return category
    return None


def resolve_entity(message: str):
    msg = clean_text(message)

    # --- config-driven routing override (single source of truth) ---
    # Each entity can declare "routing_signals" in ENTITY_REGISTRY. If a
    # signal for some entity is present in the message, that entity wins —
    # this lets e.g. status words route to leave_history even with singular
    # "leave", WITHOUT hardcoding it here. New modules just add their own
    # routing_signals to the registry; this code never changes.
    #
    # Conflict handling: "leave" (balance) and "leave_history" overlap. We
    # treat balance signals as higher-priority only when present; otherwise
    # history signals win. Generic rule: collect all entities whose signals
    # match, then prefer the one whose signal is NOT a generic balance word
    # unless a balance word is explicitly present.
    matched = []
    for entity_name, config in ENTITY_REGISTRY.items():
        signals = config.get("routing_signals", []) or []
        for sig in signals:
            if clean_text(sig) in msg:
                matched.append(entity_name)
                break

    if matched:
        # If both leave (balance) and leave_history matched, balance wins only
        # if a balance signal is literally present; else history.
        if "leave" in matched and "leave_history" in matched:
            leave_signals = ENTITY_REGISTRY.get("leave", {}).get("routing_signals", [])
            if any(clean_text(s) in msg for s in leave_signals):
                return "leave"
            return "leave_history"
        # Otherwise return the first matched entity that isn't the master
        # 'employee' (specific modules win over the generic directory).
        for m in matched:
            if m != "employee":
                return m
        return matched[0]

    best_match = None
    best_score = 0
    for entity_name, config in ENTITY_REGISTRY.items():
        aliases = config.get("aliases", [])
        for alias in aliases:
            alias_clean = clean_text(alias)
            if alias_clean in msg:
                score = len(alias_clean)
                if score > best_score:
                    best_match = entity_name
                    best_score = score
    return best_match


def compute_date_range(message, today=None):
    """Turn natural date language into a concrete (from_iso, to_iso) window.
    Covers: today / current date, tomorrow, yesterday, this|current week,
    next week, last|previous week, this|current month, next month,
    last|previous month, this|current year, and a named month (e.g. July).
    Returns ("", "") when nothing matches."""
    msg = clean_text(message)
    today = today or date.today()

    def iso(d):
        return d.isoformat()

    def month_range(y, m):
        last = calendar.monthrange(y, m)[1]
        return iso(date(y, m, 1)), iso(date(y, m, last))

    def week_range(anchor):
        start = anchor - timedelta(days=anchor.weekday())  # Monday
        return iso(start), iso(start + timedelta(days=6))

    # --- day level ---
    if re.search(r"\b(today|current date|todays|todays date|aaj)\b", msg):
        return iso(today), iso(today)
    if re.search(r"\b(tomorrow|tmrw|tomorow)\b", msg):
        d = today + timedelta(days=1)
        return iso(d), iso(d)
    if re.search(r"\byesterday\b", msg):
        d = today - timedelta(days=1)
        return iso(d), iso(d)

    # --- week level ---
    if re.search(r"\b(next week|coming week|upcoming week|agle hafte|agle week)\b", msg):
        return week_range(today + timedelta(days=7))
    if re.search(r"\b(last week|previous week|past week|pichle hafte)\b", msg):
        return week_range(today - timedelta(days=7))
    if re.search(r"\b(this week|current week|is hafte|is week)\b", msg):
        return week_range(today)

    # --- month level ---
    if re.search(r"\b(next month|coming month|upcoming month|agle mahine|agle month)\b", msg):
        y, m = (today.year + (1 if today.month == 12 else 0),
                1 if today.month == 12 else today.month + 1)
        return month_range(y, m)
    if re.search(r"\b(last month|previous month|past month|pichle mahine)\b", msg):
        y, m = (today.year - (1 if today.month == 1 else 0),
                12 if today.month == 1 else today.month - 1)
        return month_range(y, m)
    if re.search(r"\b(this month|current month|is mahine|is month)\b", msg):
        return month_range(today.year, today.month)

    # --- year level ---
    if re.search(r"\b(this year|current year|is saal)\b", msg):
        return iso(date(today.year, 1, 1)), iso(date(today.year, 12, 31))
    if re.search(r"\b(next year|coming year)\b", msg):
        return iso(date(today.year + 1, 1, 1)), iso(date(today.year + 1, 12, 31))
    if re.search(r"\b(last year|previous year)\b", msg):
        return iso(date(today.year - 1, 1, 1)), iso(date(today.year - 1, 12, 31))

    # --- a specific named month (optionally with a 4-digit year) ---
    months = ["january", "february", "march", "april", "may", "june", "july",
              "august", "september", "october", "november", "december"]
    for i, name in enumerate(months):
        if re.search(r"\b" + name + r"\b", msg) or re.search(r"\b" + name[:3] + r"\b", msg):
            ym = re.search(r"\b(20\d{2})\b", msg)
            yr = int(ym.group(1)) if ym else today.year
            return month_range(yr, i + 1)

    return "", ""


def extract_statuses(msg: str):
    statuses = []
    # synonyms first: "pending"/"awaiting"/"unapproved" all mean requested
    if any(w in msg for w in ("pending", "awaiting", "unapproved",
                              "not approved", "to be approved", "yet to")):
        statuses.append("requested")
    for status in ["approved", "rejected", "requested", "cancelled", "canceled"]:
        if status in msg:
            normalized = "cancelled" if status == "canceled" else status
            if normalized not in statuses:
                statuses.append(normalized)
    return ",".join(statuses)


def extract_time_filters(msg: str):
    filters = {}
    days = re.search(r"last\s+(\d+)\s+days?", msg)
    if days:
        filters["days"] = days.group(1)
    months = re.search(r"last\s+(\d+)\s+months?", msg)
    if months:
        filters["months"] = months.group(1)
    top = re.search(r"last\s+(\d+)\s+.*leaves?", msg)
    if not top:
        top = re.search(r"(\d+)\s+last\s+.*leaves?", msg)
    if not top:
        top = re.search(r"top\s+(\d+)", msg)
    if top and not days and not months:
        filters["top"] = top.group(1)
    return filters


def extract_date_range(msg: str):
    filters = {}
    pattern = r"between\s+(.*?)\s+to\s+(.*)"
    match = re.search(pattern, msg, re.IGNORECASE)
    if not match:
        return filters
    try:
        from_date = parser.parse(match.group(1))
        to_date = parser.parse(match.group(2))
        filters["from_date"] = from_date.strftime("%Y-%m-%d")
        filters["to_date"] = to_date.strftime("%Y-%m-%d")
    except Exception:
        pass
    return filters


def extract_year(msg: str):
    """A 4-digit year (e.g. "year 2026", "in 2026", "2026 ki leaves") ->
    a field-specific date window for that whole year. Uses date_from/date_to
    so the query builder filters the entity's date_filter_field (the leave's
    start date), not createdon."""
    m = re.search(r"\b(20\d{2})\b", msg)
    if not m:
        return {}
    year = m.group(1)
    return {"date_from": year + "-01-01", "date_to": year + "-12-31"}


def extract_experience(msg: str):
    """Parse an experience filter from natural language so it does NOT need the
    LLM. "experience more than 5 years" -> experience_gt=5; "at least 3 yrs" ->
    experience_gte; "less than 2 saal" -> experience_lt; "at most 4" ->
    experience_lte. Requires an experience/years keyword + a number."""
    msg = msg.lower()
    if not re.search(r"\bexperience\b|\bexp\b|\byears?\b|\byrs?\b|\bsaal\b", msg):
        return {}
    num = re.search(r"(\d+(?:\.\d+)?)", msg)
    if not num:
        return {}
    n = num.group(1)
    if re.search(r"more than|greater than|above|over|se\s+jyada|se\s+zyada|"
                 r"se\s+jada|se\s+jyda|jyada|zyada", msg):
        return {"experience_gt": n}
    if re.search(r"less than|fewer than|below|under|se\s+kam|se\s+km", msg):
        return {"experience_lt": n}
    if re.search(r"at least|minimum|at\s*min|or more|\+\s*$|or above", msg):
        return {"experience_gte": n}
    if re.search(r"at most|maximum|at\s*max|or less|or below", msg):
        return {"experience_lte": n}
    # bare "5 years experience" -> treat as "at least" (most common intent)
    return {"experience_gte": n}


def remove_non_name_words(text: str):
    stop_after_words = [
        "between", "from", "to", "but", "except",
        "exclude", "without", "only"
    ]
    words = text.split()
    cleaned = []
    command_set = set(COMMAND_WORDS)
    for word in words:
        if word.isdigit():
            continue
        if word in stop_after_words:
            break
        if word in command_set:
            continue
        all_types = []
        for cfg in ENTITY_REGISTRY.values():
            all_types.extend(cfg.get("allowed_types", []))
        all_types = [clean_text(x) for x in all_types]
        if clean_text(word) in all_types:
            continue
        cleaned.append(word)
    return " ".join(cleaned)


def extract_type_filters(msg: str, entity: str):
    config = ENTITY_REGISTRY.get(entity, {})
    allowed_types = config.get("allowed_types", [])
    if not allowed_types:
        return {"type": "", "types": []}
    exclude_words = [
        "exclude", "excluding", "except", "without",
        "dont show", "don't show", "do not show",
        "dont include", "don't include", "do not include",
        "hide", "remove"
    ]
    include_words = ["only", "include", "including"]
    has_exclude = any(word in msg for word in exclude_words)
    has_include = any(word in msg for word in include_words)
    if not has_exclude and not has_include:
        return {"type": "", "types": []}
    filter_type = "include" if has_include and not has_exclude else "exclude"
    found = []
    normalized = clean_text(msg)
    for item in allowed_types:
        item_clean = clean_text(item)
        if item_clean in normalized:
            found.append(item)
    return {"type": filter_type if found else "", "types": found}


def title_name(name: str):
    name = re.sub(r"\s+", " ", name).strip()
    return " ".join(part.capitalize() for part in name.split())


# Qualifier / status words that may sit before an entity word but are NOT names
NON_NAME_QUALIFIERS = {
    # greetings / fillers
    "hey", "hi", "hello", "hii", "yaar", "yar", "bhai", "bro", "plz", "pls",
    "please", "kindly", "ok", "okay", "well", "just", "also", "actually",
    "basically", "btw", "umm", "hmm",
    # pronouns / determiners
    "i", "me", "my", "mine", "myself", "we", "us", "our", "ours", "you",
    "your", "yours", "he", "him", "his", "she", "her", "hers", "they", "them",
    "their", "theirs", "it", "its", "this", "that", "these", "those",
    "who", "whom", "whose", "which", "what", "someone", "anyone", "everybody",
    # aux / common verbs
    "is", "am", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "shall", "should", "can",
    "could", "may", "might", "must", "want", "wants", "wanted", "need",
    "needs", "go", "going", "goes", "went", "get", "gets", "got", "tell",
    "tells", "show", "shows", "see", "give", "gives", "let", "make", "made",
    "take", "took", "come", "came", "know", "think", "pull", "fetch", "find",
    "display", "view", "share", "search", "check", "ahead", "forgot", "forget",
    # prepositions / conjunctions / articles
    "a", "an", "the", "for", "to", "of", "in", "on", "at", "by", "with",
    "from", "and", "or", "but", "so", "as", "if", "then", "than", "since",
    "because", "due", "while", "during", "throughout", "entire", "whole",
    "about", "into", "over", "under", "between", "out", "up", "down", "off",
    "all", "any", "some", "few", "many", "much", "more", "most", "less",
    "least", "every", "each", "no", "not", "yet", "still", "only", "really",
    "very", "too", "again", "here", "there",
    # question words / quantity
    "how", "when", "where", "why", "whether",
    # Hinglish grammar / domain fillers (never names)
    "hai", "hain", "hei", "he", "tha", "thi", "the", "kitni", "kitne", "kitna",
    "bachi", "bache", "bacha", "baki", "baaki", "book", "booking", "chutti",
    "chahiye", "chaiye", "mujhe", "meri", "mera", "mere", "batao", "bata",
    "dikhao", "dikha", "karo", "kardo", "krdo", "abhi",
    # HR domain
    "leave", "leaves", "leav", "chutti", "chhutti", "avkash",
    "sick", "annual", "casual", "comp", "compoff", "carry", "privilege",
    "earned", "maternity", "paternity", "balance", "remaining", "available",
    "used", "total", "count", "pending", "applied", "apply", "approve",
    "approved", "approving", "reject", "rejected", "cancel", "cancelled",
    "canceled", "requested", "request", "status", "history", "record",
    "records", "experience", "exp", "year", "years", "yr", "yrs", "saal",
    "month", "months", "day", "days", "week", "weeks", "today", "tomorrow",
    "yesterday", "designation", "designations", "role", "position",
    "department", "departments", "dept", "team", "manager", "managers",
    "reports", "reporting", "employee", "employees", "staff", "people",
    "colleague", "colleagues", "everyone", "user", "users", "directory",
    "profile", "details", "info", "information", "data", "list", "attendance",
    "salary", "payroll", "payslip", "reason", "half", "developer",
    "developers", "intern", "interns", "name", "named", "conflicts", "calendar",
    "type", "types", "forward", "off",
    # time-of-day / half-day qualifiers (never names)
    "morning", "afternoon", "evening", "noon", "forenoon", "midday", "night",
    "subah", "shaam", "dopahar", "raat", "halfday", "aadha", "aadhi", "aadhe",
    "beginning", "ending", "starting", "start", "end", "second", "first",
    # relative-date / Hinglish connectors
    "last", "recent", "latest", "previous", "past", "current", "next",
    "kal", "kl", "aaj", "aj", "ajj", "parso", "prso", "narso",
    "ke", "ki", "ka", "ko", "se", "mein", "par", "pe", "wala", "wali",
    "kitni", "kitne", "kitna", "bacha", "bachi", "bcha", "bachee", "abhi",
    "dikhao", "dikha", "batao", "krdo", "kardo", "kar", "kr", "lagao",
    "chahiye", "chaiye", "meri", "mera", "mere", "apni", "apna", "khud",
    "mujhe", "jo", "usko", "uska", "uski", "unki", "unka", "kuch",
    # weekdays
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "mon", "tue", "tues", "wed", "thu", "thurs", "fri", "sat", "sun",
    # months
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
    # aggregate / ranking / question words — never names
    "maximum", "max", "minimum", "min", "least", "most", "highest", "lowest",
    "top", "more", "less", "fewer", "took", "taken", "who", "whom", "whose",
    "which", "whoever", "everyone", "anybody", "somebody",
    # date / misc words that leak into queries
    "next", "last", "week", "weeks", "month", "months", "year", "years",
    "tomorrow", "today", "yesterday", "vacation", "holiday", "request",
    "requests", "coming", "upcoming", "this", "that",
    # politeness / modal / filler — never names
    "please", "kindly", "could", "would", "should", "shall", "may", "might",
    "will", "can", "go", "ahead", "let", "know", "tell", "give", "show",
    "want", "wanna", "need", "like", "view", "see", "possible", "enough",
    "able", "allowed", "afford", "mind", "around", "about", "summary",
    "recent", "total", "remaining", "pending", "me", "for", "be", "able",
    "myself", "kar", "krdo", "kardo", "dikhao", "batao", "do", "the",
}


# A bundled "not a name" vocabulary: common English words + Hindi function
# words that regularly get mis-extracted as a person's name from leftover
# tokens. This is a dictionary (one list, generalises) — not per-query tuning.
_NOT_NAME_WORDS = {
    # common english
    "left", "old", "new", "personal", "account", "employment", "organization",
    "organisation", "complete", "wise", "better", "best", "use", "used",
    "remain", "remains", "sufficient", "consecutive", "currently", "current",
    "planning", "applying", "applications", "application", "takers", "often",
    "still", "enough", "available", "any", "some", "more", "less", "much",
    "many", "number", "count", "total", "overall", "summary", "report",
    "details", "detail", "data", "information", "info", "status", "record",
    "records", "entry", "entries", "log", "logs", "activity", "transactions",
    "mistake", "everything", "anything", "something", "nothing", "latest",
    "previous", "past", "recent", "profile", "balance", "history", "leave",
    "leaves", "vacation", "holiday", "off", "day", "days", "week", "month",
    "year", "today", "tomorrow", "yesterday", "please", "kindly", "sure",
    "okay", "yes", "no", "thanks", "hello", "hey", "team", "staff", "company",
    "again", "here", "there", "this", "that", "those", "these",
    # hindi / hinglish function words
    "paas", "kaunsi", "konsi", "kaun", "kon", "maine", "meine", "jayegi",
    "jayega", "hoga", "hogi", "kya", "kyaa", "jyada", "zyada", "kam", "adhik",
    "bachi", "bache", "baki", "baaki", "bacha", "hai", "hain", "ho", "hoti",
    "hota", "se", "ka", "ki", "ke", "ko", "liya", "liye", "li", "lu", "loon",
    "mera", "meri", "mere", "mujhe", "hum", "humko", "sabse", "wale", "wala",
    "wali", "sab", "saare", "sare", "kitni", "kitne", "kitna", "chahiye",
    "chaiye", "abhi", "bhai", "yaar", "bata", "batao", "dikha", "karo",
}

# leave-domain words used to catch TYPOS ("leavs", "balnce", "pendng",
# "compof", "annul", "anual", "sik", "casul") that slip in as fake names.
_DOMAIN_FUZZY = [
    "leave", "leaves", "balance", "pending", "approved", "rejected",
    "cancelled", "annual", "casual", "sick", "compoff", "comp", "remaining",
    "available", "history", "profile", "details", "request", "requests",
    "attendance", "employee", "employees", "designation", "department",
    "experience", "manager", "vacation", "holiday",
]


def _looks_like_junk_name_token(tok):
    """True if a single extracted 'name' token is really a common/ domain /
    typo word, not a person's name."""
    low = tok.lower()
    if len(low) < 3:
        return True
    if low in _NOT_NAME_WORDS or low in NON_NAME_QUALIFIERS:
        return True
    if difflib.get_close_matches(low, _DOMAIN_FUZZY, n=1, cutoff=0.82):
        return True
    return False


def extract_employee_name(message, entity, target):
    if target == "self":
        return ""

    msg = clean_text(message)

    # Pattern 1: possessive "harshal's"  — but skip qualifier words
    match = re.search(r"(?<![a-zA-Z])([a-zA-Z]+)'s(?![a-zA-Z])", message, re.IGNORECASE)
    if match:
        cand = match.group(1)
        if clean_text(cand) not in NON_NAME_QUALIFIERS:
            return title_name(cand)

    # Pattern 2: "for <name>"
    match = re.search(r"(?<![a-zA-Z])for\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)(?![a-zA-Z])", msg)
    if match:
        name = remove_non_name_words(match.group(1).strip())
        if name and name.split()[0] not in NON_NAME_QUALIFIERS:
            return title_name(name)

    # Pattern 3: "of <name>" at end
    match = re.search(r"(?<![a-zA-Z])of\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)\s*\??\s*$", msg)
    if match:
        name = match.group(1).strip().rstrip("?")
        first = name.split()[0] if name.split() else ""
        if first not in NON_NAME_QUALIFIERS:
            return title_name(name)

    # Pattern 4a: "<name> ki/ka/ke <entity>" — Hinglish
    _NON_4A = {"show", "get", "fetch", "check", "meri", "mera", "mere",
               "apni", "apna", "mujhe", "dikhao", "batao"}
    _hm = re.search(r"([a-zA-Z]+) (ki|ka|ke) (profile|leave|balance|history|details|chutti|leaves|attendance)", msg)
    if _hm:
        _name = _hm.group(1).strip()
        if _name not in _NON_4A and _name not in NON_NAME_QUALIFIERS:
            return title_name(_name)

    # Pattern 4b: "dikhao <name> ki/ka"
    _dm = re.search(r"(dikhao|batao|dikho) ([a-zA-Z]+) (ki|ka|ke)", msg)
    if _dm:
        _name = _dm.group(2).strip()
        _NON_4B = {"mujhe", "meri", "mera", "apni", "apna", "leave",
                   "profile", "balance", "history", "details"}
        if _name not in _NON_4B and _name not in NON_NAME_QUALIFIERS and len(_name) > 2:
            return title_name(_name)

    # Pattern 4: "<name> profile/leave/balance/..."
    entity_words = {"profile", "leave", "balance", "history", "details",
                    "attendance", "salary", "info", "leaves"}
    words = msg.split()
    skip_first = {"show", "get", "fetch", "check", "find", "display",
                  "share", "give", "tell", "search", "view"}
    for i, word in enumerate(words):
        if i == 0 and word in skip_first:
            continue
        if word in skip_first:
            continue
        if i + 1 < len(words) and words[i + 1] in entity_words:
            if word in NON_NAME_QUALIFIERS:
                continue
            candidate = remove_non_name_words(word)
            if candidate and len(candidate) > 1:
                return title_name(candidate)
        if i > 0 and word in entity_words:
            prev = words[i - 1]
            if prev in NON_NAME_QUALIFIERS:
                continue
            candidate = remove_non_name_words(prev)
            if candidate and len(candidate) > 1 and prev not in skip_first:
                return title_name(candidate)

    return ""


def extract_employee_names(message):
    msg = clean_text(message)
    if " and " not in msg and "," not in msg and " or " not in msg and " aur " not in msg:
        return []
    parts = re.split(r"\s+and\s+|\s+or\s+|,", msg)
    names = []
    for part in parts:
        cleaned = remove_non_name_words(part.strip())
        name = title_name(cleaned)
        if name and len(name) > 1:
            names.append(name)
    return names


ACTION_TRIGGER_WORDS = [
    "reject", "approve", "accept", "decline",
    "deny", "cancel", "withdraw", "grant", "apply"
]


def parse_fast_intent(message: str):
    msg = clean_text(message)

    first_word = msg.split()[0] if msg.split() else ""
    if first_word in ACTION_TRIGGER_WORDS and "leave" in msg:
        return None

    small_talk = detect_small_talk(message)
    if small_talk:
        responses = {
            "greeting": [
                "Hello \U0001F44B How can I help you today?",
                "Hi there \U0001F60A How may I assist you?",
                "Hey \U0001F44B What can I do for you today?",
                "Welcome \U0001F60A How can I help?"
            ],
            "how_are_you": [
                "I'm doing great \U0001F60A How can I help you today?",
                "Doing well, thanks for asking!",
                "All good here \U0001F604 What would you like help with?",
                "I'm doing great! Ready to help with your HR queries."
            ],
            "thanks": [
                "You're welcome \U0001F60A",
                "Happy to help!",
                "Anytime \U0001F44D",
                "Glad I could help."
            ],
            "bye": [
                "Goodbye \U0001F44B Have a great day!",
                "Take care \U0001F60A",
                "See you again!",
                "Have a wonderful day ahead."
            ]
        }
        return {
            "entity": "smalltalk",
            "operation": "chat",
            "target": "self",
            "filters": {},
            "answer": random.choice(responses[small_talk])
        }

    # --- Deterministic relationship / contains queries (do NOT send these to
    # the LLM, which is unreliable for them). ---
    def _base_filters(**over):
        f = {
            "employee_name": "", "employee_names": [], "status": "", "type": "",
            "types": [], "days": "", "months": "", "top": "", "starts_with": "",
            "designation": "", "department": "", "from_date": "", "to_date": "",
            "dynamic_filters": [],
        }
        f.update(over)
        return f

    # "who is manager of <name>" / "<name>'s manager" / "who manages <name>"
    _mgr = (re.search(r"manager\s+of\s+([a-z]+(?:\s+[a-z]+)?)", msg)
            or re.search(r"who\s+manages\s+([a-z]+(?:\s+[a-z]+)?)", msg)
            or re.search(r"([a-zA-Z]+)'s\s+manager", message, re.IGNORECASE))
    if _mgr:
        _nm = " ".join(w for w in clean_text(_mgr.group(1)).split()
                       if w not in NON_NAME_QUALIFIERS).strip()
        if _nm:
            return {
                "entity": "employee", "operation": "read", "target": "employee",
                "filters": _base_filters(employee_name=title_name(_nm),
                                         want_manager=True),
                "answer": "",
            }

    # "employees whose name is related to / like / contains / matching <X>"
    _con = (re.search(r"name\s+(?:is\s+|are\s+)?(?:related\s+to|like|containing|"
                      r"contains|matching|having|with|that\s+has|jisme|jaisa)\s+"
                      r"([a-z]+)", msg)
            or re.search(r"(?:related\s+to|matching)\s+(?:name\s+)?([a-z]+)", msg))
    if _con:
        _nm = _con.group(1).strip()
        if _nm and _nm not in NON_NAME_QUALIFIERS:
            return {
                "entity": "employee", "operation": "read", "target": "multiple",
                "filters": _base_filters(employee_name_contains=title_name(_nm)),
                "answer": "",
            }

    # "what's my name" / "who am I" / "mera naam" -> show the user's own record
    if re.search(r"\b(my name|who am i|what.?s my name|mera naam|whats my name)\b", msg):
        return {
            "entity": "employee", "operation": "read", "target": "self",
            "filters": _base_filters(want_self_name=True), "answer": "",
        }

    # "who is approving / who approves / who will approve my leave", "who is my
    # approver", "who rejects my leave / who is my rejecter" -> the approver /
    # rejecter IS the user's manager.
    if re.search(r"\bapprov|\bapprover\b|\breject|\brejecter\b", msg) and (
        re.search(r"\bmy\b|\bmeri\b|\bmera\b|\bmere\b", msg)
        or re.search(r"\bwho\b|\bkaun\b|\bkon\b", msg)
    ) and not re.search(r"\b(of|for)\s+[a-z]+", msg):
        return {
            "entity": "employee", "operation": "read", "target": "self",
            "filters": _base_filters(attribute="manager"), "answer": "",
        }

    # "my manager / my department / ... / who is your manager / what is your
    # department" -> the user's OWN profile, focused on the asked attribute.
    # Full profile only when they ask for profile/details/info generally.
    if re.search(r"\b(my|meri|mera|mere|your|ur|yours)\b", msg) and re.search(
        r"\b(manager|department|dept|designation|experience|exp|profile|"
        r"code|details|info|information|reporting|joining)\b", msg
    ):
        attr = ""
        if re.search(r"\bmanager\b|\breporting\b", msg):
            attr = "manager"
        elif re.search(r"\bdepartment\b|\bdept\b", msg):
            attr = "department"
        elif re.search(r"\bdesignation\b", msg):
            attr = "designation"
        elif re.search(r"\bexperience\b|\bexp\b", msg):
            attr = "experience"
        elif re.search(r"\bcode\b", msg):
            attr = "code"
        # profile / details / info -> full profile (attr stays "")
        return {
            "entity": "employee", "operation": "read", "target": "self",
            "filters": _base_filters(attribute=attr), "answer": "",
        }

    # "who is <name>" (a person lookup) -> that employee's profile. "who is my
    # manager / who am i" are already handled above as self.
    _whois = re.search(r"^\s*who\s+is\s+([a-z]+(?:\s+[a-z]+)?)\s*\??\s*$", msg)
    if _whois:
        _nm = " ".join(w for w in clean_text(_whois.group(1)).split()
                       if w not in NON_NAME_QUALIFIERS).strip()
        if _nm:
            return {
                "entity": "employee", "operation": "read", "target": "employee",
                "filters": _base_filters(employee_name=title_name(_nm)), "answer": "",
            }

    # "show every employee / all employees / everyone / employee directory /
    # everyone in the company / all staff records" = list the whole directory.
    # One intent rule for the whole-org listing phrasings.
    if (re.search(r"\b(every|all)\b.*\b(employee|employees|staff|people|"
                  r"members?|records?)\b", msg)
            or re.search(r"\b(everyone|everybody)\b", msg)
            or re.search(r"\bemployee\s+(directory|list|master|records?)\b", msg)
            or re.search(r"\b(directory|company)\b.*\bemployee", msg)
            or "in the company" in msg or "in the organization" in msg
            or re.search(r"\bshow\s+(the\s+)?(whole|entire)\s+(team|company|org)", msg)):
        return {
            "entity": "employee", "operation": "read", "target": "multiple",
            "filters": _base_filters(), "answer": "",
        }

    # "show managers / list interns / developers / engineers ..." — a bare
    # designation (job title) means "list employees with that designation".
    # These are standard HR titles (a fixed domain vocabulary), so the CRM can
    # resolve them; routing just needs entity=employee + target=multiple.
    _DESIG = {
        "manager": "manager", "managers": "manager", "intern": "intern",
        "interns": "intern", "developer": "developer", "developers": "developer",
        "engineer": "engineer", "engineers": "engineer", "consultant": "consultant",
        "consultants": "consultant", "executive": "executive",
        "executives": "executive", "lead": "lead", "leads": "lead",
        "analyst": "analyst", "analysts": "analyst", "architect": "architect",
        "architects": "architect", "designer": "designer", "designers": "designer",
        "tester": "tester", "testers": "tester", "trainee": "trainee",
        "trainees": "trainee", "member": "team member", "members": "team member",
    }
    _dwords = set(re.findall(r"[a-z]+", msg))
    _DESIG_CANON = ["manager", "intern", "developer", "engineer", "consultant",
                    "executive", "lead", "analyst", "architect", "designer",
                    "tester", "trainee", "member"]
    _desig_hit = next((_DESIG[w] for w in re.findall(r"[a-z]+", msg) if w in _DESIG), None)
    if not _desig_hit:
        for w in re.findall(r"[a-z]+", msg):
            if len(w) >= 5:
                mm = difflib.get_close_matches(w, _DESIG_CANON, n=1, cutoff=0.82)
                if mm:
                    _desig_hit = "team member" if mm[0] == "member" else mm[0]
                    break
    # not a designation-list when it's a leave/action context, or when
    # "manager" is used relationally ("whose manager is X", "reports to X").
    if _desig_hit and not re.search(r"\b(leave|leaves|apply|approve|reject|"
                                    r"cancel|balance|history|my|mine|me)\b", msg) \
            and not re.search(r"\bmanager\s+(is|of)\b", msg) \
            and not re.search(r"\bwhose\s+manager\b", msg) \
            and not re.search(r"\breport(s|ing)?\s+to\b", msg):
        _sd = ""
        _sm = re.search(r"\b(?:designation|role|position)\s+(?:is\s+)?([a-z]+)", msg)
        return {
            "entity": "employee", "operation": "read", "target": "multiple",
            "filters": _base_filters(designation=_desig_hit), "answer": "",
        }

    # Department filters: "project department", "in sales department",
    # "project wale employees", "employees from finance", "show sales team",
    # "sales department dikhao".
    _depm = (re.search(r"\b([a-z]+)\s+department\b", msg)
             or re.search(r"\b([a-z]+)\s+dept\b", msg)
             or re.search(r"\b([a-z]+)\s+departmnt\b", msg)
             or re.search(r"\b([a-z]+)\s+deparment\b", msg)
             or re.search(r"\bdepartment\s+(?:is\s+|of\s+)?([a-z]+)", msg)
             or re.search(r"\b([a-z]+)\s+wale?\b", msg)
             or re.search(r"\b([a-z]+)\s+wali\b", msg)
             or re.search(r"\bemployees?\s+(?:from|in|under|of|working\s+in)\s+([a-z]+)", msg)
             or re.search(r"\b(?:show|list|display)\s+([a-z]+)\s+team\b", msg))
    if _depm:
        _dep = _depm.group(1).strip()
        if _dep and _dep not in ("the", "a", "an", "this", "that", "on", "my", "leave", "in"):
            return {
                "entity": "employee", "operation": "read", "target": "multiple",
                "filters": _base_filters(department=_dep), "answer": "",
            }

    # "show employees department wise / designation wise / by department"
    # = list everyone (grouping is a display concern). Handle deterministically
    # so the LLM doesn't hallucinate a bogus department filter.
    if re.search(r"(department|designation|dept)\s*-?\s*wise", msg) or \
       re.search(r"\bgroup(ed)?\s+by\s+(department|designation)\b", msg):
        return {
            "entity": "employee", "operation": "read", "target": "multiple",
            "filters": _base_filters(), "answer": "",
        }

    # --- Employee-list synonyms & common typos ---
    _EMP_FUZZY = ("employee", "employees", "emploee", "emploees", "employes",
                  "emplyee", "employe", "staff", "workforce")
    def _has_emp_word(m):
        for t in re.findall(r"[a-z]+", m):
            if t in ("workforce", "staff"):
                return True
            if len(t) >= 5 and difflib.get_close_matches(t, ["employee", "employees"], n=1, cutoff=0.8):
                return True
        return False
    if (re.search(r"\b(workforce|company\s+directory|employee\s+master|"
                  r"organization\s+employees|organisation\s+employees|all\s+users)\b", msg)
            or (re.search(r"\b(show|list|display|view|give|fetch|retrieve|plz|pls|please|all|every|sab|saare|sare)\b", msg)
                and _has_emp_word(msg) and not re.search(r"\b(my|mine|leave|leaves|balance|history|profile|attendance|of|for)\b", msg))):
        return {
            "entity": "employee", "operation": "read", "target": "multiple",
            "filters": _base_filters(), "answer": "",
        }

    # --- Employee search: "find / locate / search / lookup <name>" ---
    _srch = re.search(r"\b(find|locate|search|lookup|look up|get)\s+(?:employee\s+|staff\s+)?([a-z]+(?:\s+[a-z]+)?)\b", msg)
    if _srch and not re.search(r"\b(leave|leaves|balance|history|attendance)\b", msg):
        _cand = " ".join(w for w in _srch.group(2).split()
                         if w not in NON_NAME_QUALIFIERS and w not in ("employee", "staff", "member", "details", "information", "info"))
        if _cand:
            return {
                "entity": "employee", "operation": "read", "target": "employee",
                "filters": _base_filters(employee_name=title_name(_cand)), "answer": "",
            }

    # --- Experience filter: "experienced / senior / junior / fresher / new
    # employees", "experience filter / wise", experience-word + employees ---
    _exp_word = next((t for t in re.findall(r"[a-z]+", msg)
                      if len(t) >= 6 and difflib.get_close_matches(t, ["experience", "experienced"], n=1, cutoff=0.8)), None)
    if ((_exp_word and re.search(r"\b(filter|wise|employees?|staff|show|list|dikhao)\b", msg))
            or re.search(r"\b(experienced|senior|junior|fresher|freshers|new)\s+employees?\b", msg)
            or re.search(r"\b(senior|experienced)\s+(staff|employees?)\b", msg)):
        # a NUMERIC experience filter ("at least 3 years", "more than 5") must
        # go to the proper extractor below, not this qualitative shortcut.
        if not re.search(r"\bmy\b|\bmine\b", msg) and not re.search(r"\d", msg) \
                and not re.search(r"\b(year|years|saal|yr|yrs)\b", msg):
            return {
                "entity": "employee", "operation": "read", "target": "multiple",
                "filters": _base_filters(), "answer": "",
            }

    # --- Leave summary / analytics / overview (self OR a named employee) ---
    # "leave summary", "leave analytics", "summary of my leaves" -> own summary.
    # "summary of harshal leave", "harshal leave summary" -> that person's
    # summary (HR-only; the executor enforces authorization). NOT attendance or
    # org-wide (department/everyone) summaries — those stay coming-soon.
    if (re.search(r"\bleave", msg) or re.search(r"\bchutti", msg)) and re.search(
            r"\b(summary|analytics|overview|usage|insights?|statistics|stats|"
            r"trend|analysis|summari)\b", msg):
        if not re.search(r"\b(attendance|department|dept|team|organization|"
                         r"company|everyone|everybody|all\s+employees|compare|"
                         r"comparison|vs|versus|difference|between)\b", msg):
            # a named person -> their summary; else the current user's.
            _snm = ""
            _mn = (re.search(r"summary\s+of\s+([a-z]+(?:\s+[a-z]+)?)", msg)
                   or re.search(r"([a-z]+(?:\s+[a-z]+)?)\s+leave\s+(?:summary|analytics|overview|usage|report)", msg)
                   or re.search(r"([a-z]+)'s\s+leave", msg))
            if _mn:
                _snm = " ".join(w for w in clean_text(_mn.group(1)).split()
                                if w not in NON_NAME_QUALIFIERS
                                and not _looks_like_junk_name_token(w)).strip()
            if _snm:
                return {
                    "entity": "leave_history", "operation": "read",
                    "target": "employee",
                    "filters": _base_filters(summary=True,
                                             employee_name=title_name(_snm)),
                    "answer": "",
                }
            return {
                "entity": "leave_history", "operation": "read", "target": "self",
                "filters": _base_filters(summary=True), "answer": "",
            }

    if should_use_llm(msg):
        return None

    entity = resolve_entity(message)

    # Feasibility / projection ("can X take 5 leaves", "if X takes 3 days",
    # "enough to take 2") is ALWAYS a balance question — even when the user
    # writes the plural "leaves" (which would otherwise route to history).
    if entity in ("leave_history", "leave", None):
        _fm = clean_text(message)
        if (re.search(r"\bcan\b.*\btake\b", _fm) or re.search(r"\bif\b.*\btake", _fm)
                or "enough" in _fm or "exhaust" in _fm or "running low" in _fm
                or re.search(r"\btake\b\s+\d+", _fm)):
            entity = "leave"

    # Remaining-sense ("leaves left / remaining / available / balance / bachi")
    # is a BALANCE question, distinct from taken-sense ("how many did I take").
    # One semantic rule, not per-word patching.
    if entity in ("leave_history", "leave", None):
        _fm = clean_text(message)
        _remaining = (re.search(r"\b(left|remaining|available|balance|bachi|"
                                r"bache|baki|bacha|bचि)\b", _fm)
                      or re.search(r"\bhow (many|much)\b.*\bleaves?\b.*"
                                   r"\b(have|left|remaining|available)\b", _fm))
        _taken = re.search(r"\b(taken|took|take|used|applied|apply|history|"
                           r"previous|past|record|records|liya|li|history)\b", _fm)
        if _remaining and not _taken:
            entity = "leave"

    # History synonyms: "leave log / records / transactions / activity /
    # applications / entries / recent leaves" are the leave HISTORY, not balance.
    if entity in ("leave", "leave_history", None):
        _fm2 = clean_text(message)
        if re.search(r"\bleave", _fm2) and re.search(
            r"\b(log|logs|transaction|transactions|activity|activities|record|"
            r"records|application|applications|entries|entry|previous|past|"
            r"recent|historry|hstory)\b", _fm2) \
                and not re.search(r"\b(balance|remaining|available|left|bachi|baki)\b", _fm2):
            entity = "leave_history"

    # "comp off" / "compoff" / "comp-off" is a LEAVE TYPE. A bare "comp off"
    # request ("meri comp off batao", "vikrant comp off") means the leave
    # balance for that type, even without the word balance/leave.
    if entity in (None, "leave") and re.search(r"\bcomp\s*-?\s*off\b|\bcompoff\b", _clean := clean_text(message)):
        if not re.search(r"\b(history|log|taken|applied|record|records)\b", _clean):
            entity = "leave"

    # "my data / show my data / my hr data" = the current user's profile.
    if entity is None:
        _cm = clean_text(message)
        if re.search(r"\b(my|meri|mera|mere)\b.*\bdata\b", _cm) or _cm in ("show my data", "my data", "data"):
            entity = "employee"
            target = "self"

    if entity is None:
        _m = clean_text(message)
        if extract_experience(_m) or re.search(
            r"\b(designation|department|dept|experience|exp|manager|profile|"
            r"reporting|code|joining|email|phone)\b", _m
        ):
            entity = "employee"
        else:
            return None

    potential_employee = extract_employee_name(message, entity, "employee")

    # Strip any domain words that leaked into the name (e.g. "purav leaves" ->
    # "purav", "harshal history" -> "harshal"). NON_NAME_QUALIFIERS holds the
    # full domain vocabulary; real names are never in it.
    if potential_employee:
        potential_employee = " ".join(
            w for w in potential_employee.split()
            if clean_text(w) not in NON_NAME_QUALIFIERS
        ).strip()

    # For leave / leave_history INSIGHT, balance, feasibility or projection
    # queries that name another person ("which leave type harshal used most",
    # "can purav take 10 days annual leave", "harshal sick balance"), the name
    # rarely fits the usual patterns. Since NON_NAME_QUALIFIERS covers the
    # common vocabulary, a SINGLE leftover token is the person's name.
    if (not potential_employee and entity in ("leave", "leave_history")):
        _intent_words = ("most", "average", "avg", "trend", "how many",
                         "how much", "kitni", "kitne", "count", "total",
                         "balance", "remaining", "used", "usage", "how often",
                         "take", "exhaust", "close to", "running low",
                         "if i", "possible", "enough")
        if any(w in msg for w in _intent_words):
            _left = [w for w in re.findall(r"[a-z]+", msg)
                     if w not in NON_NAME_QUALIFIERS and len(w) > 2]
            if len(_left) == 1:
                potential_employee = title_name(_left[0])

    # List-query detection is registry-driven: any entity's "list_signals"
    # (e.g. employees/users/staff/people) mark a "multiple people" query.
    # Adding a module never requires editing this.
    _list_words = set()
    for _cfg in ENTITY_REGISTRY.values():
        for _w in _cfg.get("list_signals", []):
            _list_words.add(clean_text(_w))
    is_list_query = any(
        re.search(r"\b" + re.escape(w) + r"\b", msg) for w in _list_words if w
    )

    if potential_employee:
        target = "employee"
    elif is_list_query:
        target = "multiple"
    elif re.search(r"\b(my|mine|me)\b", msg):
        target = "self"
    else:
        target = "self"

    starts_with = ""
    starts_match = re.search(r"(?:name\s+)?(?:starts?\s+with|starting\s+with|beginning\s+with)\s+([a-zA-Z]+)", msg)
    if starts_match:
        starts_with = starts_match.group(1).upper()
        target = "multiple"

    if not starts_with:
        h_match = re.search(r"\b([a-z])\s+se\s+(?:start|shuru|suru|shuruaat)", msg)
        if h_match:
            starts_with = h_match.group(1).upper()
            target = "multiple"

    designation = ""
    desig_match = re.search(r"(?:designation|role|position)\s+(?:is\s+)?([a-z ]+?)(?:\s+in|\s+from|\s+of|$)", msg)
    if desig_match:
        designation = desig_match.group(1).strip()
        target = "multiple"

    department = ""
    dept_match = re.search(r"(?:department|dept)\s+(?:is\s+)?([a-z ]+?)(?:\s+in|\s+from|\s+of|$)", msg)
    if dept_match:
        department = dept_match.group(1).strip()
        target = "multiple"

    if not department:
        dept_match2 = re.search(r"in\s+([a-z]+)\s+department", msg)
        if dept_match2:
            department = dept_match2.group(1).strip()
            target = "multiple"

    experience = {}
    if entity == "employee":
        experience = extract_experience(msg)
        if experience:
            target = "multiple"

    filters = {
        "employee_name": "",
        "employee_names": [],
        "status": "",
        "type": "",
        "types": [],
        "days": "",
        "months": "",
        "top": "",
        "starts_with": starts_with,
        "designation": designation,
        "department": department,
        "from_date": "",
        "to_date": "",
        "dynamic_filters": []
    }

    filters.update(extract_type_filters(msg, entity))

    # Balance query naming a SINGLE leave type ("comp off balance", "sick leave
    # balance", "meri annual leave dikhao") -> show only that type. The executor
    # filters the card to it and says "not available" if the user has no such
    # balance. Canonical map keeps typos/synonyms together.
    if entity == "leave":
        _cm = clean_text(message)
        _type_map = [
            ("comp off", ["comp off", "compoff", "comp-off", "compensatory"]),
            ("carry forward", ["carry forward", "carry-forward", "carryforward", "carry"]),
            ("sick", ["sick"]),
            ("annual", ["annual"]),
            ("casual", ["casual"]),
            ("earned", ["earned"]),
            ("maternity", ["maternity"]),
            ("paternity", ["paternity"]),
        ]
        for canon, aliases in _type_map:
            if any(a in _cm for a in aliases):
                filters["only_type"] = canon
                break
    if experience:
        filters.update(experience)

    # Status / time / date extraction applies to any entity that declares a
    # status_map (currently leave_history). Registry-driven — no entity name
    # hardcoded, so a new "records" module with a status_map gets it for free.
    if ENTITY_REGISTRY.get(entity, {}).get("status_map"):
        filters["status"] = extract_statuses(msg)
        filters.update(extract_time_filters(msg))
        filters.update(extract_date_range(msg))
        filters.update(extract_year(msg))
        # relative windows: "next week", "this month", "july", "today"...
        if not filters.get("from_date") and not filters.get("to_date"):
            _fr, _to = compute_date_range(message)
            if _fr:
                filters["from_date"] = _fr
                filters["to_date"] = _to

    employee_names = extract_employee_names(message)
    # strip domain/filler tokens from each candidate (e.g. "go ahead" -> "",
    # "cancel tanish" -> "Tanish") and drop empties
    _cleaned_names = []
    for _nm in employee_names:
        _kept = " ".join(w for w in _nm.split()
                         if clean_text(w) not in NON_NAME_QUALIFIERS).strip()
        if _kept:
            _cleaned_names.append(_kept)
    employee_names = _cleaned_names
    employee_name = ""
    if len(employee_names) > 1:
        filters["employee_names"] = employee_names
        target = "multiple"
    else:
        employee_name = potential_employee

    if employee_name:
        # Final junk guard: a single-token "name" that is really a common word,
        # a Hindi function word, or a domain typo (and has no real name context
        # like "of X" / "X's" / "X ki") is NOT a person -> drop it and treat the
        # query as self. Fixes "leaves left" -> name 'Left', "show personal
        # details" -> name 'Personal', "meri leaves enough hain kya" -> 'Kya'.
        _en = employee_name.strip()
        _has_ctx = bool(re.search(
            r"\b(of|for)\s+[a-z]|[a-z]'s\b|\b(ka|ki|ke)\b|\bemployee\s+[a-z0-9]",
            msg))
        if " " not in _en and not _has_ctx and _looks_like_junk_name_token(_en):
            employee_name = ""
            filters["employee_name"] = ""
            if target == "employee":
                target = "self"

    if employee_name:
        filters["employee_name"] = employee_name
        target = "employee"

    # Safety net: a master (people) entity query with no usable filter, no
    # person, but leftover subset words (e.g. "related to harsh") -> defer to
    # Ollama, don't dump everyone. Registry-driven via is_master_entity.
    if ENTITY_REGISTRY.get(entity, {}).get("is_master_entity") and target != "self":
        has_filter = bool(
            starts_with or designation or department
            or employee_name or filters.get("employee_names")
            or experience
        )
        if not has_filter:
            entity_aliases = ENTITY_REGISTRY.get(entity, {}).get("aliases", [])
            allowed = set(COMMAND_WORDS)
            allowed.update(["all", "every", "everyone", "list", "total", "count",
                            "sabhi", "saare", "sare", "sab", "name", "named"])
            alias_words = set()
            for a in entity_aliases:
                alias_words.update(clean_text(a).split())

            def _is_allowed(w):
                if w in allowed or w in alias_words:
                    return True
                return any(w.startswith(aw) or aw.startswith(w) for aw in alias_words)

            leftover = [
                w for w in msg.split()
                if not _is_allowed(w) and not w.isdigit() and len(w) > 1
            ]
            if leftover:
                return None

    print("PRINT FILTERS :::", {
        "entity": entity,
        "operation": "read",
        "target": target,
        "filters": filters,
        "answer": ""
    })

    return {
        "entity": entity,
        "operation": "read",
        "target": target,
        "filters": filters,
        "answer": ""
    }