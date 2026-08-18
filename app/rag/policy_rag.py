"""
Policy RAG — answer questions from the organization's policy PDFs and let the
user download the source policy.

How it works (all local / offline):
  1. PDFs live in app/data/policies/ (one file per policy).
  2. On first use we extract text, split into overlapping chunks, and embed
     each chunk with Ollama's `nomic-embed-text` model. The index is cached to
     app/data/policies/.policy_index.json so we only do this once (re-run when
     the PDFs change — see `needs_reindex`).
  3. A question is embedded the same way; we cosine-rank chunks, take the best
     few, and ask the chat model to answer using ONLY those chunks.
  4. We return the answer plus the source policy's filename so the caller can
     offer a download button.

If the embedding model isn't available, we fall back to a keyword search so the
feature still works (just a bit less smart).
"""

import os
import re
import json
import math
import hashlib

try:
    import pypdf
except Exception:                       # pragma: no cover
    pypdf = None

# Reuse the app's Ollama client + models.
try:
    from app.llm.ollama_client import client as _ollama_client
except Exception:                       # pragma: no cover
    _ollama_client = None

EMBED_MODEL = "nomic-embed-text"
CHAT_MODEL = "qwen2.5:1.5b"

_HERE = os.path.dirname(os.path.abspath(__file__))
POLICY_DIR = os.path.abspath(os.path.join(_HERE, "..", "data", "policies"))
INDEX_PATH = os.path.join(POLICY_DIR, ".policy_index.json")

_CHUNK_CHARS = 1600
_CHUNK_OVERLAP = 150
_TOP_K = 4

# in-memory cache of the loaded index
_INDEX = None


# --------------------------------------------------------------------------
# PDF -> text -> chunks
# --------------------------------------------------------------------------

def _list_pdfs():
    if not os.path.isdir(POLICY_DIR):
        return []
    return sorted(
        f for f in os.listdir(POLICY_DIR)
        if f.lower().endswith(".pdf")
    )


def _pretty_title(filename):
    """'Leave_Policy_2025.pdf' -> 'Leave Policy 2025'."""
    name = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    name = re.sub(r"[_\-]+", " ", name)
    return re.sub(r"\s+", " ", name).strip().title()


def _extract_text(path):
    if pypdf is None:
        return ""
    try:
        reader = pypdf.PdfReader(path)
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts)
    except Exception:
        return ""


def _chunk(text):
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    chunks = []
    i = 0
    n = len(text)
    while i < n:
        end = min(i + _CHUNK_CHARS, n)
        # try to break on a paragraph/sentence boundary
        if end < n:
            brk = text.rfind("\n", i + int(_CHUNK_CHARS * 0.6), end)
            if brk == -1:
                brk = text.rfind(". ", i + int(_CHUNK_CHARS * 0.6), end)
            if brk != -1:
                end = brk + 1
        chunk = text[i:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        i = max(end - _CHUNK_OVERLAP, i + 1)
    return chunks


def _dir_signature():
    """A fingerprint of the PDF set (names + sizes + mtimes) so we know when to
    rebuild the index."""
    sig = []
    for f in _list_pdfs():
        p = os.path.join(POLICY_DIR, f)
        try:
            st = os.stat(p)
            sig.append(f + ":" + str(int(st.st_size)) + ":" + str(int(st.st_mtime)))
        except OSError:
            sig.append(f + ":?")
    return hashlib.md5("|".join(sig).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Embeddings (Ollama) with keyword fallback
# --------------------------------------------------------------------------

_QUERY_EMBED_CACHE = {}


def _embed(text):
    """Return a vector for `text`, or None if embeddings are unavailable."""
    if _ollama_client is None:
        return None
    # cache short query embeddings so repeated/similar asks are instant
    key = text.strip().lower()
    if len(key) < 200 and key in _QUERY_EMBED_CACHE:
        return _QUERY_EMBED_CACHE[key]
    try:
        resp = _ollama_client.embeddings(model=EMBED_MODEL, prompt=text)
        vec = resp.get("embedding") if isinstance(resp, dict) else getattr(resp, "embedding", None)
        vec = vec or None
        if vec is not None and len(key) < 200:
            _QUERY_EMBED_CACHE[key] = vec
        return vec
    except Exception:
        return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _keyword_score(query, text):
    q = set(re.findall(r"[a-z]{3,}", query.lower()))
    if not q:
        return 0.0
    t = text.lower()
    hits = sum(1 for w in q if w in t)
    return hits / len(q)


# --------------------------------------------------------------------------
# Index build / load
# --------------------------------------------------------------------------

def needs_reindex():
    if _INDEX is None:
        return True
    return _INDEX.get("signature") != _dir_signature()


def build_index(force=False):
    """Build (or rebuild) the policy index and cache it to disk."""
    global _INDEX
    sig = _dir_signature()

    if not force and os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("signature") == sig:
                _INDEX = cached
                return _INDEX
        except Exception:
            pass

    records = []
    for fname in _list_pdfs():
        path = os.path.join(POLICY_DIR, fname)
        text = _extract_text(path)
        if not text.strip():
            continue
        title = _pretty_title(fname)
        for idx, ch in enumerate(_chunk(text)):
            records.append({
                "file": fname,
                "title": title,
                "chunk_id": idx,
                "text": ch,
                "embedding": _embed(ch),   # may be None (keyword fallback)
            })

    _INDEX = {"signature": sig, "records": records,
              "has_embeddings": any(r.get("embedding") for r in records)}
    try:
        with open(INDEX_PATH, "w", encoding="utf-8") as fh:
            json.dump(_INDEX, fh)
        print("POLICY INDEX saved:", INDEX_PATH,
              "| chunks:", len(records),
              "| embeddings:", _INDEX["has_embeddings"])
    except Exception as ex:
        print("POLICY INDEX SAVE FAILED:", repr(ex), "path:", INDEX_PATH)
    return _INDEX


def _ensure_index():
    """Return the in-memory index, building it once if needed. We do NOT
    re-stat the PDF folder on every query (that was slow); once the index is in
    memory we reuse it. To pick up new/changed PDFs, restart the server (the
    startup warm-up rebuilds), or call build_index(force=True)."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    # try disk cache first, then build
    build_index()
    return _INDEX


# --------------------------------------------------------------------------
# Search + answer
# --------------------------------------------------------------------------

# words that appear in many policy filenames but aren't a TOPIC (so we drop
# them when auto-deriving keywords). Extend freely — harmless if a real word
# is here, it just won't be used as an auto-trigger on its own.
_FILENAME_STOPWORDS = {
    "policy", "policies", "the", "a", "an", "and", "or", "of", "for", "to",
    "in", "on", "with", "v1", "v2", "v3", "v1.3", "v1.5", "final", "draft",
    "new", "old", "latest", "copy", "doc", "document", "pdf",
    # org-specific boilerplate that appears on most files
    "apar", "noida", "india", "technologies", "matrix", "escalation",
    "2024", "2025", "2026", "2027", "system",
    # ambiguous words that collide with leave-action queries — never use these
    # alone as a policy trigger ("who is on leave", "apply leave")
    "leave", "leaves", "employee", "employees", "staff",
}


def policy_topic_words(min_len=4):
    """Derive topic keywords automatically from the policy PDF filenames, so
    new/renamed policies are recognised WITHOUT any code change.

    'Whistle Blower Policy.pdf'          -> {'whistle', 'blower'}
    'Anti Bribery and Corruption ...pdf' -> {'anti', 'bribery', 'corruption'}
    Common boilerplate ('policy', 'apar', 'noida', 'v1'...) is dropped.
    Returns a set of lowercase words (len >= min_len, or any multi-word bigram)."""
    words = set()
    bigrams = set()
    for fname in _list_pdfs():
        base = re.sub(r"\.pdf$", "", fname, flags=re.IGNORECASE)
        toks = [t.lower() for t in re.findall(r"[A-Za-z]+", base)]
        toks = [t for t in toks if t not in _FILENAME_STOPWORDS]
        for t in toks:
            if len(t) >= min_len:
                words.add(t)
        # adjacent pairs make good phrases: "whistle blower", "anti bribery"
        for i in range(len(toks) - 1):
            bigrams.add(toks[i] + " " + toks[i + 1])
    return words | bigrams


def list_policies():
    """[(title, filename), ...] for all indexed policies."""
    idx = _ensure_index()
    seen, out = set(), []
    for r in idx.get("records", []):
        if r["file"] not in seen:
            seen.add(r["file"])
            out.append((r["title"], r["file"]))
    return out


def _rank(query, records):
    qvec = _embed(query)
    # topic words = query minus the generic word "policy" (which is everywhere)
    raw_words = set(re.findall(r"[a-z]{3,}", query.lower()))
    generic = {"policy", "policies", "the", "for", "what", "about", "tell",
               "show", "give", "detail", "details", "rule", "rules"}
    topic_words = raw_words - generic
    q_words = raw_words
    scored = []
    for r in records:
        if qvec and r.get("embedding"):
            s = _cosine(qvec, r["embedding"])
        else:
            s = _keyword_score(query, r["text"])

        # HYBRID boost. The key signal: a chunk that is ABOUT the topic mentions
        # the topic word many times (a dedicated "Leave Policy" section says
        # "leave" 15+ times), while an unrelated policy mentions it once in
        # passing. We reward repeated topic-word hits (capped), plus title match.
        text_l = r["text"].lower()
        title_l = str(r.get("title", "")).lower()

        if topic_words:
            tokens = re.findall(r"[a-z]{3,}", text_l)
            occ = sum(tokens.count(w) for w in topic_words)
            # capped occurrence boost: 0 hits -> 0, ~6+ hits -> ~0.35 (strong
            # enough to lift a dedicated section above a passing embedding match)
            occ_boost = min(occ, 8) / 8.0 * 0.35
            coverage = sum(1 for w in topic_words if w in text_l) / len(topic_words)
            title_hits = sum(1 for w in topic_words if w in title_l) / len(topic_words)
            s += occ_boost + 0.10 * coverage + 0.25 * title_hits

        scored.append((s, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


# cache of the most recent full ranking, keyed by query — so when a query is
# first checked (query_matches_policies) and then answered
# (answer_policy_question_stream), the expensive rank runs only ONCE.
_LAST_RANK = {"query": None, "ranked": None}


def _ranked_cached(query):
    """Rank the query against all chunks, reusing the last result if it's the
    same query (case-insensitive)."""
    key = (query or "").strip().lower()
    if _LAST_RANK["query"] == key and _LAST_RANK["ranked"] is not None:
        return _LAST_RANK["ranked"]
    idx = _ensure_index()
    records = idx.get("records", [])
    ranked = _rank(query, records) if records else []
    _LAST_RANK["query"] = key
    _LAST_RANK["ranked"] = ranked
    return ranked


def search(query, top_k=_TOP_K):
    """Return the top matching chunks: [(score, record), ...]."""
    return _ranked_cached(query)[:top_k]


def _answer_with_llm(query, context_chunks):
    if _ollama_client is None:
        # no LLM -> just return the most relevant chunk text
        return context_chunks[0]["text"][:600] if context_chunks else ""
    # Use only the top 2 chunks — smaller prompt = much faster generation,
    # and the best chunk almost always holds the answer.
    top = context_chunks[:2]
    context = "\n\n---\n\n".join(
        "[" + c["title"] + "]\n" + c["text"] for c in top
    )
    prompt = (
        "You are an HR assistant. Answer the employee's question in 2-4 short "
        "sentences using ONLY the policy excerpts below. Be direct. If the "
        "answer isn't in the excerpts, say you couldn't find it in the policy "
        "documents.\n\n"
        "POLICY EXCERPTS:\n" + context + "\n\n"
        "QUESTION: " + query + "\n\nANSWER:"
    )
    try:
        resp = _ollama_client.chat(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={
                "temperature": 0.2,
                "num_predict": 160,   # shorter answer -> faster
                "num_ctx": 2048,      # smaller context -> faster
                "top_k": 20,
                "top_p": 0.9,
            },
        )
        msg = resp.get("message") if isinstance(resp, dict) else getattr(resp, "message", None)
        content = (msg or {}).get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
        return (content or "").strip()
    except Exception:
        return context_chunks[0]["text"][:600] if context_chunks else ""


# minimum relevance to consider a chunk a real match (embedding cosine)
_MIN_EMBED_SCORE = 0.35
_MIN_KEYWORD_SCORE = 0.25


def _answer_with_llm_stream(query, context_chunks):
    """Yield answer text chunk-by-chunk from the LLM (streaming). Falls back to
    yielding the best excerpt if the LLM/stream isn't available."""
    if _ollama_client is None:
        yield context_chunks[0]["text"][:600] if context_chunks else ""
        return
    top = context_chunks[:2]
    context = "\n\n---\n\n".join(
        "[" + c["title"] + "]\n" + c["text"] for c in top
    )
    prompt = (
        "You are an HR assistant. Answer the employee's question in 2-4 short "
        "sentences using ONLY the policy excerpts below. Be direct. If the "
        "answer isn't in the excerpts, say you couldn't find it in the policy "
        "documents.\n\n"
        "POLICY EXCERPTS:\n" + context + "\n\n"
        "QUESTION: " + query + "\n\nANSWER:"
    )
    try:
        stream = _ollama_client.chat(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
            options={
                "temperature": 0.2,
                "num_predict": 160,
                "num_ctx": 2048,
                "top_k": 20,
                "top_p": 0.9,
            },
        )
        got = False
        for part in stream:
            msg = part.get("message") if isinstance(part, dict) else getattr(part, "message", None)
            piece = (msg or {}).get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
            if piece:
                got = True
                yield piece
        if not got and context_chunks:
            yield context_chunks[0]["text"][:600]
    except Exception:
        if context_chunks:
            yield context_chunks[0]["text"][:600]


def answer_policy_question_stream(query):
    """Streaming variant. Yields:
        first  -> a dict {policy_title, policy_file, ...} (metadata, once)
        then   -> answer text pieces (strings)
    So the caller can send the download info up front and stream the answer."""
    hits = search(query)
    if not hits:
        yield {"no_docs": True}
        return

    idx = _ensure_index()
    use_embed = idx.get("has_embeddings") and _embed(query) is not None
    threshold = _MIN_EMBED_SCORE if use_embed else _MIN_KEYWORD_SCORE

    best_score, best_rec = hits[0]
    if best_score < threshold:
        yield {"low_conf": True}
        return

    top_file = best_rec["file"]
    context = [r for s, r in hits if r["file"] == top_file][:_TOP_K]
    # metadata first
    yield {"policy_title": best_rec["title"], "policy_file": best_rec["file"]}
    # then the streamed answer text
    for piece in _answer_with_llm_stream(query, context):
        yield piece


def answer_policy_question(query):
    """Main entry: returns a dict:
        {answer, policy_title, policy_file}  (policy_* may be None if no match)"""
    hits = search(query)
    if not hits:
        return {"answer": None, "policy_title": None, "policy_file": None,
                "no_docs": True}

    idx = _ensure_index()
    use_embed = idx.get("has_embeddings") and _embed(query) is not None
    threshold = _MIN_EMBED_SCORE if use_embed else _MIN_KEYWORD_SCORE

    best_score, best_rec = hits[0]
    if best_score < threshold:
        return {"answer": None, "policy_title": None, "policy_file": None,
                "low_conf": True}

    # keep only chunks from the top policy for a focused, citable answer
    top_file = best_rec["file"]
    context = [r for s, r in hits if r["file"] == top_file][:_TOP_K]
    answer = _answer_with_llm(query, context)
    return {
        "answer": answer,
        "policy_title": best_rec["title"],
        "policy_file": best_rec["file"],
    }


def query_matches_policies(query, min_hits=2):
    """Cheap check: does this query strongly match the policy documents? Used
    as a fallback so any topic that literally lives in a PDF is answerable even
    if it's not in the keyword lists. Returns True when the best chunk clears a
    confidence bar (embedding) OR several distinctive query words appear
    together in one chunk (keyword mode)."""
    idx = _ensure_index()
    records = idx.get("records", [])
    if not records:
        return False

    ranked = _ranked_cached(query)   # cached — reused when we answer next
    if not ranked:
        return False
    best_score = ranked[0][0]

    use_embed = idx.get("has_embeddings") and _embed(query) is not None
    if use_embed:
        # embedding cosine + boosts; a genuine topic match sits well above noise
        return best_score >= (_MIN_EMBED_SCORE + 0.05)

    # keyword mode: require a few distinctive (non-stopword) query words to
    # co-occur in the top chunk, so vague questions don't false-trigger.
    stop = {"what", "will", "the", "for", "of", "is", "are", "be", "a", "an",
            "how", "much", "many", "and", "to", "in", "do", "does", "my",
            "me", "can", "i", "you", "we", "get", "about", "tell", "show"}
    q_words = [w for w in re.findall(r"[a-z]{3,}", query.lower()) if w not in stop]
    if not q_words:
        return False
    top_text = ranked[0][1]["text"].lower()
    hits = sum(1 for w in set(q_words) if w in top_text)
    return hits >= min(min_hits, len(set(q_words)))


def policy_path(filename):
    """Absolute path to a policy PDF if it exists and is inside POLICY_DIR."""
    if not filename:
        return None
    safe = os.path.basename(filename)          # prevent path traversal
    path = os.path.join(POLICY_DIR, safe)
    if os.path.isfile(path) and path.lower().endswith(".pdf"):
        return path
    return None