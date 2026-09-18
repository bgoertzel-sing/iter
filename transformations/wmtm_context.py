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
import importlib.util, os, time, threading
from pathlib import Path

# ── Singleton state (persists across transform calls) ──────────────
_orchestrator = None
_recall_bridge = None
_store = None
_journal_clusters = None
_last_refresh = 0
_last_tick = 0
_tick_interval = 5  # run tick every 5 transforms
_refresh_interval = 300  # refresh journal every 300s (5 min)

_LOCK = threading.Lock()

def _wmtm_dir():
    """Return the directory containing the wmtm package."""
    return Path(__file__).resolve().parent.parent / "wmtm"

def _journal_module():
    """Load the _petta_journal module."""
    p = Path(__file__).resolve().parent.parent / "_petta_journal.py"
    spec = importlib.util.spec_from_file_location("_petta_journal", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def _ensure_initialized():
    """Lazy-initialize the WMTM orchestrator and recall bridge (singleton)."""
    global _orchestrator, _recall_bridge, _store, _journal_clusters, _last_refresh
    
    if _orchestrator is not None and (time.time() - _last_refresh) < _refresh_interval:
        return
    
    with _LOCK:
        if _orchestrator is not None and (time.time() - _last_refresh) < _refresh_interval:
            return
        
        # Import WMTM
        wmtm_dir = _wmtm_dir()
        import sys
        if str(wmtm_dir) not in sys.path:
            sys.path.insert(0, str(wmtm_dir))
        
        from wmtm import WMTMStore, WMTMOrchestrator
        from wmtm.recall_bridge import parse_journal, RecallBridge
        
        # Load journal
        j = _journal_module()
        lines = j._read_lines()
        resolved = j.resolve_supersedes(lines)
        clusters = parse_journal(resolved)
        
        _journal_clusters = clusters
        _last_refresh = time.time()
        
        if _orchestrator is None:
            _store = WMTMStore(capacity=60)
            _orchestrator = WMTMOrchestrator(_store)
        
        _recall_bridge = RecallBridge(clusters)

def _get_last_user_message(messages):
    """Extract the most recent user message from the message list."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            # Strip step prefix
            if content.startswith("Step "):
                # Find the second ": " to skip timestamp
                parts = content.split(": ", 2)
                if len(parts) >= 3:
                    return parts[2]
                elif len(parts) == 2:
                    return parts[1]
            return content
    return ""

def _format_active_context(store):
    """Format the WMTM active set as a context message for the LLM."""
    items = store.get_active_set()
    if not items:
        return None
    
    lines = []
    for item in items[:20]:  # top 20 by attention
        source_tag = "derived" if item.source_type == "derived" else "recalled"
        sti = item.attention.sti
        util = item.utility
        content = item.content[:200]
        lines.append(f"  [{source_tag} sti={sti:.1f} util={util:.1f}] {content}")
    
    header = f"[WMTM Active Working Memory ({len(items)} items, cycle {_orchestrator.cycle_count})]"
    body = "\n".join(lines)
    return f"{header}\n{body}"

def transform(messages, tools):
    """Run WMTM cycle and inject active context before LLM call."""
    global _last_tick
    
    try:
        _ensure_initialized()
        
        # Extract user context
        user_msg = _get_last_user_message(messages)
        
        # Recall from journal based on user context
        if user_msg and _recall_bridge:
            candidates = _recall_bridge.recall(user_msg, _store, top_k=15)
            for c in candidates:
                if c.cluster_id not in _store:
                    _store.admit(
                        c.cluster_id, c.content,
                        source_type='recalled',
                        initial_sti=max(c.score * 10, 1.0),
                    )
        
        # Read pending derives from tools (file-based IPC)
        import json, hashlib
        pending_path = Path(__file__).resolve().parent.parent / 'memory' / '_wmtm_pending_derives.jsonl'
        if pending_path.exists():
            try:
                lines = pending_path.read_text().strip().splitlines()
                pending_path.write_text('')  # clear after read
                for line in lines:
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    item_id = 'derived-' + hashlib.md5(d['note'].encode()).hexdigest()[:12]
                    if item_id not in _store:
                        _store.admit(
                            item_id, d['note'],
                            source_type='derived',
                            derived_from=d.get('source_ids', []),
                            initial_sti=20.0,
                        )
            except Exception:
                pass
        
        # Run a WMTM cycle (recall + infer + forget)
        j = _journal_module()
        result = _orchestrator.cycle(
            append_fn=lambda note: j.append_note(note),
        )
        
        # Periodic tick for attention decay
        _last_tick += 1
        if _last_tick >= _tick_interval:
            _store.tick()
            _last_tick = 0
        
        # Inject active context
        context = _format_active_context(_store)
        if context:
            messages.append({"role": "user", "content": context})
    
    except Exception as e:
        # Never break the agent loop on WMTM errors
        pass
    
    return messages, tools
