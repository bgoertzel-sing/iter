import json
import os
import time
from pathlib import Path

DESCRIPTION = "Manage workstream threads for context partitioning. Actions: create, save, switch, list, close. Usage: run(action='create', thread_id='eng-f04', state='Working on Gov Bridge tests', keywords='gov,bridge,test,petta')"

ROOT = Path(__file__).resolve().parent.parent
THREADS_DIR = ROOT / "threads"
REGISTRY_PATH = THREADS_DIR / "_registry.json"
MAX_THREADS = 5


def _load_registry():
    """Load or initialize the registry."""
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if REGISTRY_PATH.is_file():
            data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "threads" in data:
                return data
    except Exception:
        pass
    return {"activeThread": None, "threads": {}, "version": 1}


def _save_registry(registry):
    """Atomically save registry using tmp+replace (matches iter.py's save_experience pattern)."""
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = REGISTRY_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp_path), str(REGISTRY_PATH))


def _checkpoint_path(thread_id):
    """Get the checkpoint file path for a thread."""
    return THREADS_DIR / (thread_id + ".md")


def _save_checkpoint(thread_id, state):
    """Write checkpoint file atomically."""
    path = _checkpoint_path(thread_id)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(state, encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def run(action, thread_id="", state="", keywords=""):
    """
    action: create|save|switch|list|close
    thread_id: identifier for the thread (e.g. 'eng-f04', 'discuss-iter-arch')
    state: checkpoint content — summary of current work state, pending items, key context
    keywords: comma-separated keywords for experience filtering (e.g. 'gov,bridge,test,petta')
    """
    registry = _load_registry()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if action == "create":
        if not thread_id:
            return "ERROR: thread_id is required for create"
        if thread_id.startswith("_"):
            return "ERROR: thread_id cannot start with underscore"
        if thread_id in registry["threads"]:
            return f"ERROR: thread '{thread_id}' already exists. Use save to update or close+create to replace."

        active_count = sum(1 for t in registry["threads"].values() if t.get("status") == "active")
        if active_count >= MAX_THREADS:
            return f"ERROR: max {MAX_THREADS} active threads. Close a thread first."

        kw_list = [k.strip().lower() for k in keywords.split(",") if k.strip()] if keywords else []

        registry["threads"][thread_id] = {
            "status": "active",
            "keywords": kw_list,
            "stateFile": str(_checkpoint_path(thread_id)),
            "createdAt": ts,
            "updatedAt": ts
        }
        registry["activeThread"] = thread_id
        _save_registry(registry)

        if state:
            _save_checkpoint(thread_id, state)

        return f"SUCCESS: Created thread '{thread_id}' (keywords: {kw_list}). Now active. {'Checkpoint saved.' if state else 'No checkpoint yet — use save to add one.'}"

    elif action == "save":
        if not thread_id:
            thread_id = registry.get("activeThread", "")
        if not thread_id:
            return "ERROR: no active thread and no thread_id specified"
        if thread_id not in registry["threads"]:
            return f"ERROR: thread '{thread_id}' does not exist. Use create first."
        if not state:
            return "ERROR: state is required for save — provide a summary of current work"

        _save_checkpoint(thread_id, state)
        registry["threads"][thread_id]["updatedAt"] = ts

        if keywords:
            kw_list = [k.strip().lower() for k in keywords.split(",") if k.strip()]
            registry["threads"][thread_id]["keywords"] = kw_list

        _save_registry(registry)
        return f"SUCCESS: Checkpoint saved for thread '{thread_id}' ({len(state)} chars)"

    elif action == "switch":
        if not thread_id:
            return "ERROR: thread_id is required for switch"
        if thread_id not in registry["threads"]:
            return f"ERROR: thread '{thread_id}' does not exist. Use create first."
        if registry["threads"][thread_id].get("status") != "active":
            return f"ERROR: thread '{thread_id}' is closed. Use create to reopen."

        # Auto-save previous thread if state is provided
        prev = registry.get("activeThread")
        if prev and prev != thread_id and state:
            _save_checkpoint(prev, state)
            registry["threads"][prev]["updatedAt"] = ts

        registry["activeThread"] = thread_id
        _save_registry(registry)

        checkpoint = ""
        cp_path = _checkpoint_path(thread_id)
        if cp_path.is_file():
            checkpoint = cp_path.read_text(encoding="utf-8").strip()

        return f"SUCCESS: Switched to thread '{thread_id}'. {'Previous thread auto-saved.' if (prev and prev != thread_id and state) else ''} Checkpoint: {checkpoint[:500] if checkpoint else '(none)'}"

    elif action == "list":
        if not registry["threads"]:
            return "No threads created yet. Use create to start one."

        lines = []
        active = registry.get("activeThread")
        for tid, info in registry["threads"].items():
            marker = " [ACTIVE]" if tid == active else ""
            status = info.get("status", "unknown")
            kw = ", ".join(info.get("keywords", []))
            updated = info.get("updatedAt", "?")
            cp_path = _checkpoint_path(tid)
            has_cp = cp_path.is_file()
            lines.append(f"  {tid}{marker} ({status}) keywords=[{kw}] updated={updated} checkpoint={'yes' if has_cp else 'no'}")

        return "Threads:\n" + "\n".join(lines)

    elif action == "close":
        if not thread_id:
            return "ERROR: thread_id is required for close"
        if thread_id not in registry["threads"]:
            return f"ERROR: thread '{thread_id}' does not exist"

        registry["threads"][thread_id]["status"] = "closed"
        registry["threads"][thread_id]["closedAt"] = ts

        if registry.get("activeThread") == thread_id:
            # Switch to another active thread or None
            other_active = [t for t, info in registry["threads"].items()
                          if info.get("status") == "active" and t != thread_id]
            registry["activeThread"] = other_active[0] if other_active else None

        _save_registry(registry)
        return f"SUCCESS: Thread '{thread_id}' closed. Active thread: {registry.get('activeThread', 'none')}"

    else:
        return f"ERROR: Unknown action '{action}'. Valid: create, save, switch, list, close"
