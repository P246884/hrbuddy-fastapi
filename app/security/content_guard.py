"""
app/security/content_guard.py

Deterministic guard for vulgar / explicit / NSFW input so such messages get a
fixed, professional redirect instead of being sent to the LLM (which is slow
and unpredictable for these).

Design notes:
- Matching is word-boundary based on cleaned (lowercased, punctuation-stripped)
  text, so common words are NOT false-flagged:
  "analysis"/"analytics", "class"/"assistant", "Gandhi", "london", "errand".
- The list deliberately EXCLUDES legitimate HR vocabulary like
  "sexual harassment", "harassment", "complaint" — genuine workplace concerns
  must always get through.
"""

import re
from app.intent.fast_intent import clean_text


# Single vulgar / explicit words and phrases (English + Hinglish romanized).
# Each is matched with word boundaries (see _PATTERNS), so substrings inside
# normal words do not trigger.
_BLOCK_TERMS = [
    # explicit sexual acts / phrases
    "anal sex", "blow job", "blowjob", "hand job", "handjob", "anal",
    "bdsm", "porn", "nude", "nudes", "horny", "orgasm", "dildo",
    "masturbate", "masturbation",
    # explicit body-part slang
    "dick", "pussy", "vagina", "penis", "boobs", "tits", "cock", "cunt",
    # general profanity
    "fuck", "fucking", "fucker", "motherfucker", "asshole", "bastard",
    # Hinglish profanity / vulgar slang
    "lund", "lauda", "lawda", "loda", "lond",
    "chut", "choot", "chutiya", "chutiye", "chutia",
    "gaand", "gand", "gandu",
    "bhosdi", "bhosadi", "bhosdike", "bhosdiwale",
    "madarchod", "madarchood", "maderchod",
    "behenchod", "bhenchod", "bsdk",
    "randi", "raand", "jhaat", "jhaant",
]

_PATTERNS = [re.compile(r"\b" + re.escape(t) + r"\b") for t in _BLOCK_TERMS]


_SAFE_REPLY = (
    "I can only help with HR-related queries — for example employee details, "
    "leave balances, leave history, and applying or approving leaves. "
    "Please ask something related to that and I'll be glad to help."
)


def is_inappropriate(message: str) -> bool:
    """Return True if the (cleaned) message contains a blocked term."""
    msg = clean_text(message or "")
    if not msg:
        return False
    return any(pattern.search(msg) for pattern in _PATTERNS)


def safe_redirect_message() -> str:
    return _SAFE_REPLY