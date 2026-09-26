import json
import os
import time
from pathlib import Path

DESCRIPTION = "Thread-partitioned context: injects active thread checkpoint, filters experience by thread affinity"

ROOT = Path(__file__).resolve().parent.parent
THREADS_DIR = ROOT / "threads"
REGISTRY_PATH = THREADS_DIR / "_registry.json"
LOG_PATH = THREADS_DIR / "_classification.log"

# How many recent messages to always keep regardless of thread affinity
ALWAYS_KEEP_RECENT = 10

# Minimum keyword overlap ratio to consider a message "on-thread"
ON_THREAD_THRESHOLD = 0.15


def _load_registry():
    """Load registry, return None on any failure."""
    try:
        if REGISTRY_PATH.is_file():
            data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def _load_checkpoint(registry):
    """Load the active thread's checkpoint content, or None."""
    active = registry.get("activeThread")
    if not active:
        return None, None
    threads = registry.get("threads", {})
    thread_info = threads.get(active)
    if not thread_info:
        return active, None
    state_file = thread_info.get("stateFile", "")
    if not state_file:
        state_file = str(THREADS_DIR / (active + ".md"))
    path = Path(state_file)
    if not path.is_absolute():
        path = ROOT / path
    try:
        if path.is_file():
            return active, path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return active, None


def _get_keywords(registry, thread_id):
    """Get keywords for the active thread."""
    threads = registry.get("threads", {})
    thread_info = threads.get(thread_id, {})
    kw = thread_info.get("keywords", [])
    if isinstance(kw, str):
        kw = [w.strip() for w in kw.split(",") if w.strip()]
    return [k.lower() for k in kw if isinstance(k, str)]


def _message_affinity(content, keywords):
    """
    Compute how much a message's content relates to the active thread.
    Returns a float 0.0-1.0.
    """
    if not keywords or not content:
        return 0.5  # neutral when no keywords or no content
    content_lower = content.lower()
    hits = sum(1 for kw in keywords if kw in content_lower)
    return hits / len(keywords)


def _find_tool_call_ids(msg):
    """Extract tool_call_ids from an assistant message's tool_calls."""
    ids = set()
    for tc in msg.get("tool_calls", []):
        if isinstance(tc, dict):
            tc_id = tc.get("id")
            if tc_id:
                ids.add(tc_id)
    return ids


def _log_decision(thread_id, total, kept, removed):
    """Append a one-line classification log entry."""
    try:
        THREADS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} thread={thread_id} total={total} kept={kept} removed={removed}\n"
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def transform(messages, tools):
    """
    ABI: transform(messages, tools) -> (messages, tools)
    
    1. Read threads/_registry.json
    2. If active thread has a checkpoint, inject it after the system message
    3. Filter experience messages by keyword affinity to the active thread
    4. Always preserve: system messages, recent N messages, current input,
       and complete tool_call/tool_result groups
    5. On any error, return messages unchanged (graceful degradation)
    """
    registry = _load_registry()
    if registry is None:
        return messages, tools

    active_thread, checkpoint = _load_checkpoint(registry)
    if not active_thread:
        return messages, tools

    keywords = _get_keywords(registry, active_thread)

    # --- Step 1: Inject checkpoint after the first system message ---
    injected = False
    new_messages = []
    for msg in messages:
        new_messages.append(msg)
        if not injected and msg.get("role") == "system":
            if checkpoint:
                new_messages.append({
                    "role": "user",
                    "content": (
                        f"[ACTIVE THREAD: {active_thread}] "
                        f"Context checkpoint:\n{checkpoint}"
                    )
                })
            else:
                new_messages.append({
                    "role": "user",
                    "content": (
                        f"[ACTIVE THREAD: {active_thread}] "
                        f"No checkpoint saved yet. Use thread(action=\"save\", ...) to checkpoint."
                    )
                })
            injected = True

    if not keywords:
        # No keywords configured — inject checkpoint but skip filtering
        return new_messages, tools

    # --- Step 2: Filter experience by thread affinity ---
    # Identify message regions:
    #   - System messages (index 0 typically): always keep
    #   - Checkpoint injection (index 1 if injected): always keep
    #   - Experience messages: filter by affinity
    #   - Temporary/current messages at the end: always keep

    # Find the boundary between "preamble" and "experience"
    # Preamble = system messages + our injected checkpoint
    preamble_end = 0
    for i, msg in enumerate(new_messages):
        if msg.get("role") == "system":
            preamble_end = i + 1
        elif msg.get("content", "").startswith("[ACTIVE THREAD:"):
            preamble_end = i + 1
        else:
            break

    # The last few messages are the current turn (temporary_message from iter.py)
    # These always have "Step YYYY-MM-DD" prefix and are the most recent user inputs
    # We always keep the last ALWAYS_KEEP_RECENT messages
    total = len(new_messages)
    keep_from = max(preamble_end, total - ALWAYS_KEEP_RECENT)

    # Process the "experience" region (between preamble and keep_from)
    experience_start = preamble_end
    experience_end = keep_from

    if experience_start >= experience_end:
        # Not enough experience to filter
        return new_messages, tools

    # Build a set of messages to keep/remove in the experience region
    # First pass: score each message
    experience = new_messages[experience_start:experience_end]
    scores = []
    for msg in experience:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            scores.append(1.0)  # always keep system messages
        elif role == "tool":
            scores.append(-1.0)  # handle with tool_call grouping
        else:
            scores.append(_message_affinity(content, keywords))

    # Second pass: identify tool_call groups (assistant + matching tool results)
    # An assistant message with tool_calls must be kept with all its tool results
    keep_indices = set()
    tool_call_groups = {}  # tool_call_id -> assistant_message_index

    for i, msg in enumerate(experience):
        role = msg.get("role", "")
        if role == "assistant":
            tc_ids = _find_tool_call_ids(msg)
            for tc_id in tc_ids:
                tool_call_groups[tc_id] = i
        elif role == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id and tc_id in tool_call_groups:
                # This tool result belongs to a group
                assistant_idx = tool_call_groups[tc_id]
                # If the assistant message scores well, keep the whole group
                if scores[assistant_idx] >= ON_THREAD_THRESHOLD:
                    keep_indices.add(assistant_idx)
                    keep_indices.add(i)

    # Third pass: decide keep/remove for non-tool messages
    for i, (msg, score) in enumerate(zip(experience, scores)):
        role = msg.get("role", "")
        if role == "system":
            keep_indices.add(i)
        elif role == "tool":
            # Already handled via grouping above
            tc_id = msg.get("tool_call_id")
            if tc_id and tc_id in tool_call_groups:
                assistant_idx = tool_call_groups[tc_id]
                if scores[assistant_idx] >= ON_THREAD_THRESHOLD:
                    keep_indices.add(i)
                # else: remove with its assistant message
            else:
                # Orphan tool result — keep it (safe default)
                keep_indices.add(i)
        elif role == "assistant":
            tc_ids = _find_tool_call_ids(msg)
            if tc_ids:
                # Already handled in grouping pass
                if i in keep_indices:
                    pass  # already kept
                elif score >= ON_THREAD_THRESHOLD:
                    keep_indices.add(i)
                    # Also keep all its tool results
                    for j, m in enumerate(experience):
                        if m.get("role") == "tool" and m.get("tool_call_id") in tc_ids:
                            keep_indices.add(j)
                # else: remove the assistant + its tool results
            else:
                # Plain assistant message (content only, no tool calls)
                if score >= ON_THREAD_THRESHOLD:
                    keep_indices.add(i)
        elif role == "user":
            if score >= ON_THREAD_THRESHOLD:
                keep_indices.add(i)

    # Build filtered experience
    filtered_experience = [experience[i] for i in sorted(keep_indices)]
    removed_count = len(experience) - len(filtered_experience)

    # Reassemble: preamble + filtered_experience + always-keep tail
    result = (
        new_messages[:preamble_end]
        + filtered_experience
        + new_messages[keep_from:]
    )

    _log_decision(active_thread, len(experience), len(filtered_experience), removed_count)

    return result, tools
