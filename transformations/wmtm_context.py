"""WMTM context injection transformation.

Initializes WMTM (Working Medium-Term Memory) and injects the active set
as context before each LLM call. The WMTM recalls relevant memories from
the PeTTa journal, runs inference (deduction/induction/abduction), decays
attention, and forgets stale items — creating a living working memory
between STM and LTM.

The transformation also handles:
- Periodic tick() for attention decay and forgetting
- Turn-end writeback of high-utility items to LTM journal
- Derived belief admission from inference engine

Design: the WMTM orchestrator is a singleton, initialized lazily on first
call. The journal is parsed once and cached, with periodic refreshes.
"""
import importlib.util, json as _json, os, os as _os, time, threading
from pathlib import Path

_WMTM_STATE_FILE = Path(__file__).resolve().parent.parent / 'memory' / '_wmtm_state.json'

# ── Singleton state (persists across transform calls) ──────────────
_orchestrator = None
_recall_bridge = None
_store = None
_journal_clusters = None
_last_refresh = 0
# Note: store.tick() is called inside orchestrator.cycle(), no separate tick needed
_refresh_interval = 300  # refresh journal every 300s (5 min)

_LOCK = threading.Lock()
_cached_journal_mod = None

def _wmtm_dir():
    """Return the directory containing the wmtm package."""
    return Path(__file__).resolve().parent.parent / "wmtm"

def _journal_module():
    """Load the _petta_journal module (cached after first import)."""
    global _cached_journal_mod
    if _cached_journal_mod is not None:
        return _cached_journal_mod
    p = Path(__file__).resolve().parent.parent / "_petta_journal.py"
    spec = importlib.util.spec_from_file_location("_petta_journal", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    _cached_journal_mod = m
    return m

def _ensure_store_ready():
    """Create orchestrator + restore persisted state.  Fast path (<1 s).

    Does NOT parse the PeTTa journal — that is deferred to
    _ensure_journal_ready() and only triggered for real user messages.
    Since the iter process restarts every step, parsing the full journal
    on every automated-loop tick was the root cause of the 30 s timeout.
    """
    global _orchestrator, _store

    if _orchestrator is not None:
        return

    with _LOCK:
        if _orchestrator is not None:
            return

        # Import WMTM
        wmtm_dir = _wmtm_dir()
        import sys
        if str(wmtm_dir) not in sys.path:
            sys.path.insert(0, str(wmtm_dir))

        from wmtm import WMTMStore, WMTMOrchestrator

        _store = WMTMStore(capacity=60)
        _orchestrator = WMTMOrchestrator(_store)
        # F01: Restore WMTM state from previous process if available
        try:
            if _os.path.exists(_WMTM_STATE_FILE):
                with open(_WMTM_STATE_FILE) as f:
                    snapshot = _json.load(f)
                _orchestrator.restore_state(snapshot)
                # F01c: Keep module-level _store in sync after restore.
                _store = _orchestrator.store
        except Exception as e:
            print(f"[wmtm_context] WARN: state restore failed: {type(e).__name__}: {e}", file=sys.stderr)


def _ensure_journal_ready():
    """Parse the PeTTa journal and (re)create the recall bridge.

    This is the SLOW path — potentially many seconds for large journals.
    Only called when a real user message needs recall; skipped entirely
    for automated iter-loop prompts.
    """
    global _recall_bridge, _journal_clusters, _last_refresh

    if _recall_bridge is not None and (time.time() - _last_refresh) < _refresh_interval:
        return

    with _LOCK:
        if _recall_bridge is not None and (time.time() - _last_refresh) < _refresh_interval:
            return

        from wmtm.recall_bridge import parse_journal, RecallBridge

        j = _journal_module()
        lines = j._read_lines()
        resolved = j.resolve_supersedes(lines)
        clusters = parse_journal(resolved)

        _journal_clusters = clusters
        _last_refresh = time.time()
        _recall_bridge = RecallBridge(clusters)

# Automated prompt patterns that should not drive WMTM recall
_AUTOMATED_PATTERNS = [
    "NO ADDITIONAL USER INPUT",
    "CONTINUE THE CURRENT USER TASK",
    "HEARTBEAT_OK",
    "HEARTBEAT",
    "RUNTIME ERROR",
    "RUNTIMEERROR",       # Python class name (no space) after step-prefix stripping
    "TIMEOUT AFTER 30S",  # iter timeout injection
    "YOUR PREVIOUS RESPONSE CONTAINED NO TOOL CALL",  # F24: iter non-delivery injection
    "NOT DELIVERED",       # F24: iter delivery failure injection
    "CALL AT LEAST ONE TOOL",  # F24: iter tool-call enforcement
    "WAS NOT DELIVERED",   # F24: alternate delivery failure phrasing
    "IF YOU INTENDED THIS CONTENT AS COMMUNICATION",  # F24: iter send suggestion
    # F30: remaining iter.py injections that were misread as real user input
    # (iter.py lines ~1035-1105). They drove the full path on autonomous
    # ticks, so passive/feedback utility inflated items endlessly.
    "NO NEW USER INPUT",
    "CONTINUE AUTONOMOUS WORK",
    "TASK COMPLETED. DO NOT RE-SEND",
    "OUTPUT TOKEN LIMIT REACHED",
    "TOOL LIMIT REACHED",
    "MEMORY FOLDER TOTAL CHARACTER CAPACITY",
    "[BACKGROUND_BRANCH_",
]

def _is_automated_prompt(text):
    """Return True if text is an automated iter-loop prompt, not real user input."""
    upper = text.upper().strip()
    for pat in _AUTOMATED_PATTERNS:
        if pat in upper:
            return True
    # Also detect the bracketed form: [NO ADDITIONAL USER INPUT. CONTINUE...]
    if upper.startswith("[NO ADDITIONAL") or upper.startswith("[CONTINUE"):
        return True
    return False

def _parse_item_id_timestamp(item_id):
    """Parse a date from an item ID and return a Unix timestamp, or 0.0.

    F26b: Mirrors the recall_bridge._parse_cluster_timestamp logic but runs
    in the fast path without importing wmtm. Handles:
    - ISO compact: ep-note-20260918T061756Z
    - Date-only: fact-evlink3-2026-09-03
    - Compact YYYYMMDD: ep-20260903010647219173
    """
    import re
    # Format 1: ISO compact
    m = re.search(r'(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z', item_id)
    if m:
        try:
            import calendar, datetime
            dt = datetime.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6)),
            )
            return calendar.timegm(dt.timetuple())
        except (ValueError, OverflowError):
            pass

    # Format 2: YYYY-MM-DD
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', item_id)
    if m:
        try:
            import calendar, datetime
            dt = datetime.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                12, 0, 0,
            )
            ts = calendar.timegm(dt.timetuple())
            if 1577836800 < ts < 1893456000:  # 2020-2030
                return ts
        except (ValueError, OverflowError):
            pass

    # Format 3: Compact YYYYMMDD prefix
    m = re.search(r'(20\d{2})(\d{2})(\d{2})\d{6,}', item_id)
    if m:
        try:
            import calendar, datetime
            dt = datetime.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                12, 0, 0,
            )
            ts = calendar.timegm(dt.timetuple())
            if 1577836800 < ts < 1893456000:
                return ts
        except (ValueError, OverflowError):
            pass

    return 0.0


def _is_duplicate_topic(content, existing_snippets, threshold=0.6):
    """Return True if content is too similar to any existing snippet.
    
    Uses Jaccard similarity on word sets to detect topically redundant items.
    This prevents the WMTM from being filled with 15 variations of the same
    observation (e.g., "send() fails because processing/ is empty").
    """
    if not existing_snippets:
        return False
    content_words = set(content.lower().split())
    if len(content_words) < 3:
        return False
    for snippet in existing_snippets:
        snippet_words = set(snippet.lower().split())
        if not snippet_words:
            continue
        intersection = content_words & snippet_words
        union = content_words | snippet_words
        jaccard = len(intersection) / len(union) if union else 0
        if jaccard >= threshold:
            return True
    return False

def _cleanup_stale_items(store, cycle_count, max_zero_utility_cycles=10):
    """Evict stale items based on utility, wall-clock age, and content quality.
    
    F14b+F15+F16: Multi-tier cleanup using WALL-CLOCK TIME, not cycle count.
    
    The iter process restarts every step, so cycle-based age never exceeds 1.
    Wall-clock timestamps (item.created_at) are the only reliable staleness
    measure.
    
    F16 FIX: Recalled journal items (source_type='recalled') use much longer
    thresholds than derived items. The old 1-hour Tier 2 evicted recalled items
    before they could accumulate utility, causing an infinite evict→re-admit
    loop: cleanup evicts zero-utility items after 1hr → journal recall re-admits
    them with fresh timestamps → next restart they're >1hr old → evicted again.
    Items never accumulated utility because they were constantly being reset.
    
    Thresholds:
    DERIVED items:
      1. Operational noise with zero utility → evict immediately
      2. Zero-utility derived items older than 1 hour → evict
      3. Low-utility derived items (util < 3.0) older than 24 hours → evict
      4. Derived items older than 72 hours with util < 5.0 → evict
      5. Trivially short derived content (< 20 chars) → evict
    
    RECALLED items (journal knowledge — the base context):
      1. Operational noise → evict immediately (even recalled noise is noise)
      2. Zero-utility recalled items → keep for 48 hours (not 1 hour!)
      3. Low-utility recalled items → keep for 7 days
      4. Hard cap: 14 days without high utility → evict
    
    BOTH:
      - Legacy items with created_at=0 and zero utility → evict
    """
    import time as _t
    now = _t.time()
    
    to_evict = []
    for item in list(store.get_active_set()):
        # F25: Use effective_age_seconds which considers origin_timestamp
        # (the journal entry's real date) instead of just created_at
        # (re-admission time). This prevents 23-day-old entries from
        # appearing "41 minutes old" after re-admission.
        if hasattr(item, 'effective_age_seconds'):
            wall_age_hours = item.effective_age_seconds / 3600.0
        else:
            wall_age_hours = (now - item.created_at) / 3600.0 if item.created_at > 0 else float('inf')
        is_recalled = (item.source_type == 'recalled')
        
        # Tier 0: Operational noise with zero utility → immediate eviction
        # (applies to ALL items, including recalled — noise is noise)
        if item.utility <= 0.0 and _is_operational_noise(item.content):
            to_evict.append(item.id)
            continue
        
        # Tier 1: Legacy items with no timestamp (created_at=0) and zero utility
        if item.created_at == 0.0 and item.utility <= 0.0:
            to_evict.append(item.id)
            continue
        
        if is_recalled:
            # F16: Recalled items get MUCH longer thresholds.
            # They're journal knowledge, not ephemeral operational state.
            # The old 1-hour threshold caused the evict-readmit loop.
            
            # Recalled Tier 2: Zero utility after 48 hours → stale recall
            if item.utility <= 0.0 and wall_age_hours >= 48.0:
                to_evict.append(item.id)
                continue
            
            # Recalled Tier 3: Low utility after 7 days → outlived relevance
            if item.utility < 3.0 and wall_age_hours >= 168.0:  # 7 * 24
                to_evict.append(item.id)
                continue
            
            # Recalled Tier 4: Hard cap — 14 days without high utility
            if wall_age_hours >= 336.0 and item.utility < 5.0:  # 14 * 24
                to_evict.append(item.id)
                continue
        else:
            # Derived items keep the original aggressive thresholds
            
            # Derived Tier 2: Zero utility + older than 1 hour = stale noise
            if item.utility <= 0.0 and wall_age_hours >= 1.0:
                to_evict.append(item.id)
                continue
            
            # Derived Tier 3: Low utility + older than 24 hours
            if item.utility < 3.0 and wall_age_hours >= 24.0:
                to_evict.append(item.id)
                continue
            
            # Derived Tier 4: 72 hours without high utility
            if wall_age_hours >= 72.0 and item.utility < 5.0:
                to_evict.append(item.id)
                continue
            
            # Derived Tier 5: Trivially short content (garbage inference output)
            if len(item.content) < 20:
                to_evict.append(item.id)
                continue
    
    for item_id in set(to_evict):
        store.evict(item_id)

def _is_operational_noise(content):
    """Return True if content is clearly operational noise, not useful knowledge.
    
    Detects patterns like repeated ChannelContractError observations,
    "no additional user input" status updates, and similar ephemeral
    operational state that should not persist in working memory.
    """
    lower = content.lower()
    noise_patterns = [
        "channelcontracterror",
        "processing/ directory is empty",
        "processing/ is empty",
        "no active user request",
        "no additional user input",
        "send() fails by design",
        "send() correctly refuses",
        "send() requires exactly 1",
        "awaiting user input",
        "no pending user task",
        "all autonomous work complete",
        "task complete",
        "nothing left to do",
        # F16b: PeTTa batch monitoring noise
        "consolidation pass",
        "memory consolidation",
        "pass 52",
        "pass 56",
        "pass 79",
        "pass 48",
        "pass 73",
        "pass 80",
        "swipl processes",
        "state t (stopped",
        "run_manifest.json",
        "watchdog relaunch",
        "petta-memory state",
        "system critical",
        # F16b: Iter channel mechanics noise
        "channel mechanics verified",
        "send is request-bound",
        "receive moves inbox->processing",
    ]
    matches = sum(1 for pat in noise_patterns if pat in lower)
    # If content matches 2+ noise patterns, it's operational noise
    # F16b: Also flag single-match items that are clearly dated operational logs
    if matches >= 2:
        return True
    # Single-match items that start with timestamps or pass numbers
    if matches >= 1 and (lower.startswith("pass ") or lower.startswith("consolidation") 
                         or "petta" in lower and ("state" in lower or "memory" in lower)):
        return True
    return False

_GROUP_CTX_CLOSE = "</external_untrusted_telegram_group_context>"
_REPLY_MARKER_RE = __import__("re").compile(r"^\s*\[reply to your message \d+\]\s*")


def _extract_direct_request(content):
    """F30: Return only the text addressed to us, without channel context.

    Channel messages embed the recent group chat in an
    <external_untrusted_telegram_group_context> block before the actual
    request. Recall used the whole thing as the query, so a short question
    ("how is WMTM going?") was drowned out by pages of hive-appliance
    review text. Keep only what follows the closing tag, and drop the
    "[reply to your message N]" marker. Fall back to the full text when
    nothing follows the block.
    """
    if _GROUP_CTX_CLOSE in content:
        tail = content.rsplit(_GROUP_CTX_CLOSE, 1)[1].strip()
        if tail:
            content = tail
    # F35: attached documents (PDF/zip text, up to ~140k chars) made recall,
    # response feedback and relevance stimulus tokenize and score the whole
    # attachment -> 30 s TIMEOUT (Sep 29, WMTM report zip). Keep only the
    # document file names and cap the query text.
    content = _DOC_BLOCK_RE.sub(_doc_placeholder, content)
    if len(content) > _DIRECT_REQUEST_MAX_CHARS:
        content = content[:_DIRECT_REQUEST_MAX_CHARS]
    return _REPLY_MARKER_RE.sub("", content).strip()


_DIRECT_REQUEST_MAX_CHARS = 2000
_DOC_BLOCK_RE = __import__("re").compile(
    r"<external_untrusted_document>.*?(?:</external_untrusted_document>|\Z)",
    __import__("re").S)
_DOC_NAME_RE = __import__("re").compile(r'"file_name":\s*"([^"]{1,200})"')


def _doc_placeholder(m):
    names = _DOC_NAME_RE.findall(m.group(0)[:2000])
    return "[attached document: " + (", ".join(names) or "unnamed") + "]"


def _classify_user_content(content):
    """Return "" for automated/injected content, else the direct request text."""
    # ── normalise: strip "Step YYYY-MM-DD HH:MM:SS: " prefix ──
    if content.startswith("Step "):
        parts = content.split(": ", 1)
        if len(parts) == 2:
            content = parts[1]
    # F30b: check injections BEFORE stripping the [channel] tag, otherwise
    # "[BACKGROUND_BRANCH_ABANDONED] ..." is mistaken for a channel tag and
    # its remainder ("branch_id=...") is treated as a real user request.
    if _is_automated_prompt(content):
        return ""
    # ── strip optional [channel] tag (e.g. "[protocosmo2] ") ──
    if content.startswith("[") and "] " in content[:40]:
        bracket_end = content.index("] ")
        content = content[bracket_end + 2:]
    if _is_automated_prompt(content):
        return ""
    for prefix in ("[RUNTIME ERROR", "RuntimeError", "[YOUR PREVIOUS RESPONSE", "[NOT DELIVERED"):
        if content.startswith(prefix):
            return ""
    return _extract_direct_request(content)


def _get_last_user_message(messages):
    """Extract the most recent real user message.

    Returns empty string for automated prompts / runtime errors so the
    fast path (no wmtm imports) is used.

    F22 FIX: Only inspect the SINGLE most-recent non-injection user
    message.  The old code skipped automated messages but then scanned
    backwards through the entire conversation history, eventually finding
    an old real user question.  That triggered the full slow path (wmtm
    import + journal parsing = 10-25 s) on every automated tick, causing
    the 30 s TIMEOUT loop.

    Now: skip our own WMTM context injections, then the FIRST non-
    injection user message we find is the decision point.  If it is
    automated or a runtime-error injection → return "".  If it is real
    → return the content.  We never look any further back.
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")

            # Skip our own WMTM context injections (these are not user input)
            if "[WMTM Active Working Memory" in content:
                continue

            # F30: normalisation + decision shared with response feedback
            return _classify_user_content(content)
    return ""


def _response_answered_real_message(messages, assistant_idx):
    """F30: True if the assistant message at assistant_idx replied to a real
    user message (not an automated tick). Walks back to the nearest user
    message, skipping our own WMTM context injections."""
    for msg in reversed(messages[:assistant_idx]):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "") or ""
        if "[WMTM Active Working Memory" in content:
            continue
        return bool(_classify_user_content(content))
    return False

def _check_response_feedback(messages, store, orchestrator):
    """Check LLM response AND user query for references to WMTM items.
    
    F15b: Expanded feedback matching. Items get utility boosts when:
    1. The LLM response references them (strongest signal — item was useful)
    2. The user's query matches item content (item is topically relevant)
    
    Thresholds lowered from 30%/3-match to 20%/2-match because:
    - Short responses rarely hit 30% overlap with long WMTM items
    - In the automated loop, responses are often <20 chars ("NO_REPLY")
    - Even 2 distinctive keyword matches indicate topical relevance
    
    User-query matching uses a weaker boost (1.5 vs 3.0) since the item
    being topically relevant is less certain than the LLM actually using it.
    """
    # Collect text to match against
    texts_to_check = []
    
    # Last assistant response (strongest signal)
    # F30: only count it when that response answered a real user message.
    # Status replies on automated ticks restate whatever WMTM showed, so
    # counting them made items reinforce themselves (Sep 20 note: 327 util).
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if msg.get("role") == "assistant":
            resp = msg.get("content", "") or ""
            if resp and len(resp) >= 20 and _response_answered_real_message(messages, idx):
                texts_to_check.append(("response", resp))
            break
    
    # Last real user message (weaker signal — topical relevance)
    user_msg = _get_last_user_message(messages)
    if user_msg and len(user_msg) >= 20:
        texts_to_check.append(("query", user_msg))
    
    if not texts_to_check:
        return
    
    for item in store.get_active_set():
        content = item.content
        if len(content) < 20:
            continue
        
        # Extract distinctive words from the item
        item_words = set(w.strip(".,;:!?()[]{}\"'").lower()
                        for w in content.split() if len(w) > 3)
        distinctive = item_words - _COMMON_WORDS
        if not distinctive:
            continue
        
        matched = False
        for source_type, text_content in texts_to_check:
            if matched:
                break
            text_lower = text_content.lower()
            text_words = set(text_lower.split())
            
            # Strategy 1: Distinctive keyword overlap (lowered thresholds)
            overlap = distinctive & text_words
            overlap_ratio = len(overlap) / len(distinctive)
            # F15b: 20% overlap with 2+ matches (was 30%/3)
            if overlap_ratio >= 0.20 and len(overlap) >= 2:
                boost = 3.0 if source_type == "response" else 1.5
                item.attention.boost(boost)
                orchestrator.utility.record_use(item.id, orchestrator._cycle)
                matched = True
                break
            
            # Strategy 2: Substring match on distinctive phrases
            for phrase in _extract_distinctive_phrases(content):
                if phrase in text_lower:
                    boost = 3.0 if source_type == "response" else 1.5
                    item.attention.boost(boost)
                    orchestrator.utility.record_use(item.id, orchestrator._cycle)
                    matched = True
                    break


# Common words filtered from response-feedback matching
_COMMON_WORDS = {
    # English
    "the", "this", "that", "with", "from", "have", "been", "will", "would",
    "could", "should", "about", "after", "before", "into", "over", "under",
    "between", "through", "during", "without", "also", "just", "only",
    "very", "more", "most", "some", "each", "every", "other", "another",
    "such", "much", "many", "still", "already", "even", "back", "then",
    "when", "where", "what", "which", "while", "since", "because", "like",
    "make", "made", "does", "done", "need", "needs", "used", "using",
    "work", "working", "check", "checked", "checking", "found", "find",
    # Tech generic
    "file", "files", "path", "data", "code", "test", "tests", "item",
    "items", "value", "values", "state", "store", "error", "status",
    "result", "results", "function", "method", "class", "module", "system",
    "process", "running", "active", "current", "updated", "update",
    "fixed", "added", "removed", "changed", "config", "memory", "cycle",
}


def _extract_distinctive_phrases(content):
    """Extract distinctive phrases from content for substring matching.
    
    Yields short, specific substrings that are unlikely to appear by
    coincidence: commit hashes, error names, specific identifiers,
    file paths, numeric values with context.
    """
    import re
    # Commit hashes (7+ hex chars)
    for m in re.finditer(r'\b([0-9a-f]{7,40})\b', content.lower()):
        yield m.group(1)
    # Error/class names (CamelCase or UPPER_CASE)
    for m in re.finditer(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', content):
        yield m.group(1).lower()
    for m in re.finditer(r'\b([A-Z_]{4,})\b', content):
        yield m.group(1).lower()
    # File paths
    for m in re.finditer(r'(?:[a-zA-Z_][a-zA-Z0-9_]*/){2,}[a-zA-Z_][a-zA-Z0-9_.]*', content):
        yield m.group(0).lower()
    # Specific numbers with context (e.g., "276 receipts", "g1025")
    for m in re.finditer(r'\b(g\d{4}|\d{3,}/\d{3,}|pid\s*\d+)\b', content.lower()):
        yield m.group(1)


def _relevance_stimulus(store, bridge, user_msg, orch=None, rent=0.5):
    """F33b: ECAN-style stimulus for items already in working memory.

    recall() skips clusters already active, so resident items never saw the
    current message and old-topic items kept their STI. For a real user
    message: score every resident recalled item against it, inject STI scaled
    to the current attention scale (top relevance -> max resident STI + 1), and
    charge non-matching items rent (they lose a rent fraction of STI). Items
    whose cluster left the resolved journal (superseded) are evicted. Ranking
    stays ECAN-driven: nothing bypasses STI.
    """
    try:
        qterms = bridge._tokenize(user_msg)
        if not qterms:
            return {}
        items = [it for it in store.get_active_set() if it.source_type == 'recalled']
        active_ids = {it.id for it in store.get_active_set()}
        scores = {}
        for it in items:
            cl = bridge._by_id.get(it.origin_cluster or it.id)
            if cl is None:
                ev = store.evict(it.id)
                if ev is not None and orch is not None:
                    orch.forget_log.record(ev, orch._cycle)
                continue
            scores[it.id] = bridge._score_cluster(cl, qterms, active_ids, store)
        if not scores:
            return {}
        smax = max(scores.values())
        top_sti = max((it.attention.sti for it in store.get_active_set()), default=1.0)
        for it in items:
            s = scores.get(it.id)
            if s is None:
                continue
            if s > 0 and smax > 0:
                it.attention.boost((s / smax) * (top_sti + 1.0))
            else:
                it.attention.penalty(it.attention.sti * rent)
        return scores
    except Exception as e:
        import sys
        print(f"[wmtm_context] WARN relevance stimulus: {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def _format_active_context(store):
    """Format the WMTM active set as a context message for the LLM.
    
    F14c+F15: Shows wall-clock age (hours/days) instead of cycle-based age
    since cycle count never exceeds 1 due to process restarts. Items capped
    at 15 to reduce context noise.
    """
    import time as _t
    now = _t.time()
    items = store.get_active_set()
    if not items:
        return None
    
    display_items = items[:15]
    
    lines = []
    for item in display_items:
        source_tag = "derived" if item.source_type == "derived" else "recalled"
        sti = item.attention.sti
        util = item.utility
        # F25: Show wall-clock age based on effective age (considers origin_timestamp)
        if hasattr(item, 'effective_age_seconds'):
            age_secs = item.effective_age_seconds
        elif item.created_at > 0:
            age_secs = now - item.created_at
        else:
            age_secs = -1
        if age_secs >= 0:
            if age_secs < 3600:
                age_str = f"{age_secs/60:.0f}m"
            elif age_secs < 86400:
                age_str = f"{age_secs/3600:.1f}h"
            else:
                age_str = f"{age_secs/86400:.1f}d"
        else:
            age_str = "?"
        content = item.content[:200]
        lines.append(f"  [{source_tag} sti={sti:.1f} util={util:.1f} age={age_str}] {content}")
    
    total = len(items)
    shown = len(display_items)
    header = f"[WMTM Active Working Memory ({total} items, showing top {shown}, cycle {_orchestrator.cycle_count})]"
    body = "\n".join(lines)
    return f"{header}\n{body}"

def _fast_path_automated(messages, tools):
    """Ultra-lightweight path for automated iter-loop prompts.

    F20: The full wmtm package import (inference engine, PLN, recall bridge,
    GoalChainer bindings) takes 10-25s on cold start because the iter process
    restarts every step. For automated prompts with no real user input, we
    bypass ALL wmtm imports and read the persisted state JSON directly.

    This path:
    1. Reads _wmtm_state.json (pure stdlib json, no wmtm imports)
    2. Formats the active items as context
    3. Applies minimal attention decay to the state
    4. Saves back
    5. Returns in <1s
    """
    try:
        if not _os.path.exists(_WMTM_STATE_FILE):
            return messages, tools

        with open(_WMTM_STATE_FILE) as f:
            snapshot = _json.load(f)

        store_data = snapshot.get("store", {})
        items_dict = store_data.get("items", {})
        if not items_dict:
            return messages, tools

        # Format active context directly from JSON (no wmtm objects needed)
        import time as _t
        now = _t.time()
        cycle = snapshot.get("cycle", 0)

        # Sort by STI descending, cap at 15
        sorted_items = sorted(
            items_dict.values(),
            key=lambda it: it.get("attention", {}).get("sti", 0),
            reverse=True,
        )[:15]

        lines = []
        for item in sorted_items:
            src = item.get("source_type", "unknown")
            att = item.get("attention", {})
            sti = att.get("sti", 0)
            util = item.get("utility", 0)
            created = item.get("created_at", 0)
            # F25: Use origin_timestamp for age display when available
            origin_ts = item.get("origin_timestamp", 0)
            display_ts = created
            if origin_ts > 0:
                display_ts = min(origin_ts, created) if created > 0 else origin_ts
            if display_ts > 0:
                age_secs = now - display_ts
                if age_secs < 3600:
                    age_str = f"{age_secs/60:.0f}m"
                elif age_secs < 86400:
                    age_str = f"{age_secs/3600:.1f}h"
                else:
                    age_str = f"{age_secs/86400:.1f}d"
            else:
                age_str = "?"
            content = item.get("content", "")[:200]
            lines.append(f"  [{src} sti={sti:.1f} util={util:.1f} age={age_str}] {content}")

        total = len(items_dict)
        shown = len(sorted_items)
        header = f"[WMTM Active Working Memory ({total} items, showing top {shown}, cycle {cycle})]"
        context = header + "\n" + "\n".join(lines)
        messages.append({"role": "user", "content": context})

        # Minimal state update: decay STI, increment cycle, cleanup stale items
        # F23: Wall-clock cleanup in fast path. Without this, stale items
        # persist forever because cleanup only ran in the full path (real
        # user messages), and nearly all cycles are automated.
        evict_ids = []
        for item_id, item_data in items_dict.items():
            att = item_data.get("attention", {})
            decay = att.get("sti_decay", 0.9)
            att["sti"] = att.get("sti", 0) * decay

            # F25+F26b: Wall-clock staleness check.
            # Priority: origin_timestamp > ID-parsed date > created_at.
            # origin_timestamp is the journal entry's real date; created_at is the
            # re-admission time. F26b adds ID-based date parsing as fallback for
            # items admitted before F25 (no origin_timestamp in state file).
            created = item_data.get("created_at", 0)
            origin_ts = item_data.get("origin_timestamp", 0)
            util = item_data.get("utility", 0)
            is_recalled = item_data.get("source_type") == "recalled"

            # F26b: Parse date from item ID when origin_timestamp is missing
            if origin_ts <= 0:
                origin_ts = _parse_item_id_timestamp(item_id)

            # Use the oldest known timestamp for age calculation
            effective_ts = created
            if origin_ts > 0:
                effective_ts = min(origin_ts, created) if created > 0 else origin_ts
            
            if effective_ts > 0:
                age_hours = (now - effective_ts) / 3600.0
            else:
                age_hours = float('inf')

            # Same thresholds as _cleanup_stale_items:
            if is_recalled:
                # 14-day hard cap for recalled items with utility < 5.0
                if age_hours >= 336.0 and util < 5.0:
                    evict_ids.append(item_id)
                # F26b: 7-day threshold for low-utility recalled items
                # (was missing from fast path — only _cleanup_stale_items had it)
                elif util < 3.0 and age_hours >= 168.0:
                    evict_ids.append(item_id)
                # 48h zero-utility recalled items
                elif util <= 0.0 and age_hours >= 48.0:
                    evict_ids.append(item_id)
            else:
                # 72h hard cap for derived items with utility < 5.0
                if age_hours >= 72.0 and util < 5.0:
                    evict_ids.append(item_id)
                # 1h zero-utility derived items
                elif util <= 0.0 and age_hours >= 1.0:
                    evict_ids.append(item_id)

        for eid in evict_ids:
            del items_dict[eid]

        snapshot["cycle"] = cycle + 1

        # Save updated state
        with open(_WMTM_STATE_FILE, "w") as f:
            _json.dump(snapshot, f)

    except Exception as e:
        import sys
        print(f"[wmtm_context] WARN fast-path: {type(e).__name__}: {e}", file=sys.stderr)

    return messages, tools


def transform(messages, tools):
    """Run WMTM cycle and inject active context before LLM call.

    F18: Time-budgeted execution. The iter transformation timeout is 30s.
    Heavy operations (inference, PLN, GoalChainer) are skipped when:
    - The prompt is automated (no real user input)
    - The time budget (20s) has been exhausted by earlier phases
    This prevents timeout crashes in the automated loop while preserving
    full inference for real user interactions.
    """
    _t0 = time.time()

    def _budget_ok(limit=20.0):
        """Return True if we have time budget remaining."""
        return (time.time() - _t0) < limit

    try:
        # F20: Detect automated prompts BEFORE any wmtm imports.
        # The full wmtm package import chain (inference, PLN, recall bridge)
        # takes 10-25s on cold start. For automated prompts, bypass it entirely.
        user_msg = _get_last_user_message(messages)
        is_automated = not user_msg  # automated prompts return ""

        if is_automated:
            return _fast_path_automated(messages, tools)

        # Full path: create store + restore persisted state
        _ensure_store_ready()

        if not _budget_ok():
            # Store init alone consumed the budget — just inject context
            context = _format_active_context(_orchestrator.store) if _orchestrator else None
            if context:
                messages.append({"role": "user", "content": context})
            return messages, tools

        store = _orchestrator.store

        # Response feedback: boost items the LLM actually referenced
        if _budget_ok():
            _check_response_feedback(messages, store, _orchestrator)

        # Recall from journal based on user context
        # Only parse journal for real user messages (slow path)
        if user_msg and _budget_ok():
            _ensure_journal_ready()
        if user_msg and _recall_bridge and _budget_ok():
            candidates = _recall_bridge.recall(user_msg, store, top_k=15)
            # Topic diversity: limit similar items to avoid WMTM pollution
            admitted_snippets = []
            for c in candidates:
                if c.cluster_id not in store:
                    # Check topic diversity — skip if too similar to already-admitted items
                    if _is_duplicate_topic(c.content, admitted_snippets):
                        continue
                    # F25: Pass the cluster's original journal timestamp so staleness
                    # checks use the real age, not re-admission age. Without this,
                    # a 23-day-old GoalChainer entry re-admitted 41 minutes ago
                    # appears "41 minutes old" and evades the 48h eviction threshold.
                    origin_ts = _recall_bridge.get_cluster_timestamp(c.cluster_id)
                    store.admit(
                        c.cluster_id, c.content,
                        source_type='recalled',
                        initial_sti=max(c.score * 10, 1.0),
                        origin_timestamp=origin_ts,
                    )
                    admitted_snippets.append(c.content)
            # F33b: ECAN relevance stimulus (shared with wmtm_eval harness).
            _relevance_stimulus(store, _recall_bridge, user_msg, _orchestrator)

        # Read pending derives from tools (file-based IPC)
        if _budget_ok():
            import json, hashlib
            pending_path = Path(__file__).resolve().parent.parent / 'memory' / '_wmtm_pending_derives.jsonl'
            if pending_path.exists():
                try:
                    import fcntl
                    # Read + clear under the same flock the writer uses, so an
                    # append landing between read and clear is not lost.
                    with open(pending_path, 'r+') as pf:
                        fcntl.flock(pf, fcntl.LOCK_EX)
                        try:
                            lines = pf.read().strip().splitlines()
                            pf.seek(0)
                            pf.truncate()
                        finally:
                            fcntl.flock(pf, fcntl.LOCK_UN)
                    for line in lines:
                        if not line.strip():
                            continue
                        d = json.loads(line)
                        item_id = 'derived-' + hashlib.md5(d['note'].encode()).hexdigest()[:12]
                        if item_id not in store:
                            store.admit(
                                item_id, d['note'],
                                source_type='derived',
                                derived_from=d.get('source_ids', []),
                                initial_sti=20.0,
                            )
                except Exception:
                    pass

        # Stale cleanup: evict zero-utility items that have survived too many cycles
        if _budget_ok():
            _cleanup_stale_items(store, _orchestrator.cycle_count)

        # F18: Run the full WMTM cycle (recall + infer + PLN + forget + writeback)
        # ONLY for real user messages AND if we have time budget remaining.
        # The cycle runs inference (deduction/induction/abduction), PLN, and
        # GoalChainer — each can take 5-15s. For automated prompts (iter loop
        # with no real user input), skip the cycle entirely: just inject the
        # existing active set as context. This prevents the 30s timeout.
        if not is_automated and _budget_ok(limit=15.0):
            j = _journal_module()
            result = _orchestrator.cycle(
                append_fn=lambda note: j.append_note(note),
            )
        elif _budget_ok():
            # Automated prompt or tight budget: do lightweight tick only
            # (decay attention, age items) without inference/PLN/writeback
            evicted = store.tick()
            for ev in evicted:
                _orchestrator.forget_log.record(ev, _orchestrator._cycle)
            _orchestrator.utility.tick(store, _orchestrator._cycle)
            _orchestrator._cycle += 1

        # Inject active context and give displayed items a small passive
        # utility boost. F17: Items shown to the LLM accumulate utility
        # over time even if the LLM doesn't explicitly reference them.
        context = _format_active_context(_orchestrator.store)
        if context:
            messages.append({"role": "user", "content": context})
            # F17: Passive utility for displayed items.
            # FIX: The old direct assignment (item.utility += 0.3) was overwritten
            # by utility.tick() which sets item.utility = rec.utility_score.
            # Using record_use() properly increments use_count in the tracker,
            # so utility_score (use_count * 2.0) accumulates across restarts.
            displayed = _orchestrator.store.get_active_set()[:15]
            for item in displayed:
                _orchestrator.utility.record_use(item.id, _orchestrator._cycle)

    except Exception as e:
        # F02: Log WMTM context errors instead of silently swallowing them.
        # Never break the agent loop on WMTM errors, but surface failures for debugging.
        import sys
        print(f"[wmtm_context] WARNING: {type(e).__name__}: {e}", file=sys.stderr)

    # F01: Persist WMTM state for next subprocess
    try:
        if _orchestrator is not None:
            snapshot = _orchestrator.snapshot_state()
            # F01d: Validate snapshot before writing — catch serialization
            # issues that were previously swallowed by except:pass, leaving
            # the state file as "{}" (empty) and losing all WMTM state.
            if snapshot and snapshot.get("store", {}).get("items"):
                _json.dump(snapshot, open(_WMTM_STATE_FILE, "w"))
            elif snapshot:
                # Snapshot exists but store is empty — log but still save
                # (could be legitimate after aggressive forgetting)
                import sys
                print(f"[wmtm_context] WARN: saving snapshot with 0 store items "
                      f"(cycle={snapshot.get('cycle', '?')})", file=sys.stderr)
                _json.dump(snapshot, open(_WMTM_STATE_FILE, "w"))
    except Exception as e:
        # F01d: Log persistence errors instead of silently swallowing them.
        # The old except:pass caused state files to be "{}" and all WMTM
        # state to be lost on process restart.
        import sys
        print(f"[wmtm_context] ERROR persisting state: {type(e).__name__}: {e}", file=sys.stderr)
    
    return messages, tools
