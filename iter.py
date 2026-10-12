import copy
import datetime
import hashlib
import inspect
import json
import os
import queue
import re
import sys
import openai
import time
import importlib.util
import signal
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

# --------------------------------------------------------------------
# 0. Configuration:
# --------------------------------------------------------------------
MAX_MEMORY_CHARS = 3000 #When memory turns into an index
LLM_TIMEOUT = 600
KEEP_REASONING_IN_EPISODE = True
PRINT_CALLS = False
MAX_TOOL_CALLS = 10
MAX_TOOL_OUTPUT_CHARS = 5000
MAX_EXPERIENCE_SIZE = 100   #20 percent
RETAIN_EXPERIENCE_SIZE = 80 #jumps
# BEGIN PROGRESS_NOT_PAUSE (2026-10-04; port of the VM2 change 20261003T1600Z,
# Ben Telegram 7445/7462, glicerico 7473).  The old foreground stop at
# MAX_FAST_STEPS=50 and the background-branch stop at ITER_BRANCH_STEP_BUDGET=25
# ended long work with a checkpoint message.  Now every ITER_PROGRESS_EVERY steps
# of a request (or after ITER_PROGRESS_MAX_SILENT_SECONDS without an update) the
# agent posts a NON-closing interim note (send final=false) and keeps working;
# if the model does not post one within ITER_PROGRESS_FALLBACK_STEPS steps, Iter
# posts a mechanical note itself (no model call).  Only runaway backstops stop
# a request, each with its reason: the hard ceiling, repeated identical steps,
# and steps that produce no new results.  Foreground and every background
# branch keep their own StepGuard.  Idle stays provider-silent (unchanged).
def _env_int(name, default, minimum):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)

PROGRESS_EVERY = _env_int("ITER_PROGRESS_EVERY", 30, 3)  # interim update cadence (steps)
PROGRESS_FALLBACK_STEPS = _env_int("ITER_PROGRESS_FALLBACK_STEPS", 2, 1)  # mechanical note if the model has not posted within this many steps of the nudge
PROGRESS_MAX_SILENT_SECONDS = _env_int("ITER_PROGRESS_MAX_SILENT_SECONDS", 420, 60)  # also nudge after this long without an update (the receiver waits --iter-timeout=900 s per update)
HARD_STEP_CEILING = _env_int("ITER_HARD_STEP_CEILING", 300, 3)  # runaway backstop per request
REPEAT_STEPS = _env_int("ITER_REPEAT_STEPS", 8, 2)  # backstop: consecutive steps with identical tool calls + arguments
STALL_STEPS = _env_int("ITER_STALL_STEPS", 12, 2)  # backstop: consecutive steps whose tool calls/results were all seen before in this request
MAX_FAST_STEPS = HARD_STEP_CEILING  # legacy name
TURN_WATCHDOG_THRESHOLD = HARD_STEP_CEILING - 2  # legacy name: the step-ceiling warning step
# END PROGRESS_NOT_PAUSE

# --- M1 Step 1.1: Tier-1 mechanical checkpoint (flag-guarded, default ON) ---
ITER_CHECKPOINT_ENABLED = os.getenv("ITER_CHECKPOINT_ENABLED", "1") == "1"
CHECKPOINT_TOOL_SNAPSHOT = 5       # number of recent tool calls to include
CHECKPOINT_OUTPUT_CHARS = 200     # truncate each tool output to this many chars
CHECKPOINT_DIR = Path("checkpoints")  # root dir for checkpoint files
CHECKPOINT_CHANNEL = os.getenv("ITER_CHECKPOINT_CHANNEL", "protocosmo2")

# --- M2 Step 2.1: Threaded LLM call wrapper (flag ITER_CONCURRENCY_ENABLED, default OFF) ---
ITER_CONCURRENCY_ENABLED = os.getenv("ITER_CONCURRENCY_ENABLED", "0") == "1"
ITER_PROMOTE_SECONDS = int(os.getenv("ITER_PROMOTE_SECONDS", "30"))
# --- M3 Step 3.1: BACKGROUND_DEADLINE (R13: bounded background lifetime) ---
BACKGROUND_DEADLINE = max(2 * ITER_PROMOTE_SECONDS, 300)
# --- M3 Step 3.5: Branch step budget + branch checkpoint queuing (R20) ---
# PROGRESS_NOT_PAUSE: was ITER_BRANCH_STEP_BUDGET=25 (a stop); a branch now runs under the
# same per-request StepGuard backstops as the foreground, bounded by the hard ceiling.
BRANCH_STEP_BUDGET = HARD_STEP_CEILING
# Multi-branch: up to ITER_MAX_BACKGROUND_BRANCHES promoted branches run at once
# (default 3, clamped to 1..MAX_BACKGROUND_BRANCHES_LIMIT). 1 = the old single slot.
MAX_BACKGROUND_BRANCHES_LIMIT = 8

def _max_background_branches(raw=None):
    """Parse the configured branch cap; invalid values fall back to 1 (the old behaviour)."""
    raw = os.getenv("ITER_MAX_BACKGROUND_BRANCHES", "3") if raw is None else raw
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"ITER_MAX_BACKGROUND_BRANCHES invalid ({raw!r}); using 1")
        value = 1
    return max(1, min(MAX_BACKGROUND_BRANCHES_LIMIT, value))

ITER_MAX_BACKGROUND_BRANCHES = _max_background_branches()
# Request binding: with concurrency on, every turn/branch answers only the request it
# was started from (tools/send.py reads ITER_REQUEST_IDS). Was "> 1 branch" until
# 2026-10-12: with 1 branch and no binding the channel holds one request, so a chat
# message could not be claimed while a promoted branch still owned the open request.
ITER_REQUEST_BINDING = ITER_CONCURRENCY_ENABLED
if ITER_REQUEST_BINDING:
    # one foreground request + one per background branch may be open at once
    os.environ.setdefault("ITER_MAX_OPEN_REQUESTS", str(ITER_MAX_BACKGROUND_BRANCHES + 1))
_foreground_request_ids = {}  # channel stem -> request_id owned by the foreground turn

# Multi-branch: cap on concurrent LLM calls (foreground + background branches).
# Default = ITER_MAX_BACKGROUND_BRANCHES + 1, i.e. no extra limit; set
# ITER_MAX_CONCURRENT_LLM_CALLS lower to protect the provider. Clamped to 1..default.
def _max_concurrent_llm_calls(raw=None):
    default = ITER_MAX_BACKGROUND_BRANCHES + 1
    raw = os.getenv("ITER_MAX_CONCURRENT_LLM_CALLS", "") if raw is None else raw
    if str(raw).strip() == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"ITER_MAX_CONCURRENT_LLM_CALLS invalid ({raw!r}); using {default}")
        return default
    return max(1, min(default, value))

ITER_MAX_CONCURRENT_LLM_CALLS = _max_concurrent_llm_calls()
_llm_call_semaphore = threading.BoundedSemaphore(ITER_MAX_CONCURRENT_LLM_CALLS)

def _capped_llm_create(llm_client, **kwargs):
    """chat.completions.create under the concurrency cap; waits at most LLM_TIMEOUT for a slot."""
    if not _llm_call_semaphore.acquire(timeout=LLM_TIMEOUT):
        raise Exception(f"LLM concurrency cap: no slot free within {LLM_TIMEOUT}s "
                        f"(ITER_MAX_CONCURRENT_LLM_CALLS={ITER_MAX_CONCURRENT_LLM_CALLS})")
    try:
        return llm_client.chat.completions.create(**kwargs)
    finally:
        _llm_call_semaphore.release()

# Hung-call fix (bg-89ff37ae): the 300 s idle deadline used to drop a branch whose single
# LLM call was still in flight. Now an in-flight call is bounded by its own limits
# (slot wait + client timeout), and a promoted branch retries a timed-out/disconnected call.
def _bg_llm_attempts(raw=None):
    """Parse ITER_BG_LLM_ATTEMPTS (default 2 = one retry); invalid -> 2, clamped to 1..5."""
    raw = os.getenv("ITER_BG_LLM_ATTEMPTS", "2") if raw is None else raw
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"ITER_BG_LLM_ATTEMPTS invalid ({raw!r}); using 2")
        value = 2
    return max(1, min(5, value))

BG_LLM_ATTEMPTS = _bg_llm_attempts()
BG_INFLIGHT_LIMIT = 2 * LLM_TIMEOUT + 60  # slot wait (LLM_TIMEOUT) + client timeout (LLM_TIMEOUT) + grace
BG_RETRY_BACKOFF_CAP = 60

def _bg_llm_create(llm_client, result_container, **kwargs):
    """_capped_llm_create for branch threads: marks the call in flight (so the idle
    deadline waits for it) and, once promoted, retries retryable provider errors
    (timeout, disconnect, 429/5xx; not quota exhaustion) up to BG_LLM_ATTEMPTS."""
    attempt = 0
    while True:
        attempt += 1
        result_container["llm_inflight_since"] = time.time()
        try:
            return _capped_llm_create(llm_client, **kwargs)
        except Exception as error:
            result_container.pop("llm_inflight_since", None)
            if (not result_container.get("branch_id") or attempt >= BG_LLM_ATTEMPTS
                    or result_container.get("abandoned")
                    or not retryable_provider_error(error) or provider_quota_exhausted(error)):
                raise
            wait = min(RETRY_BACKOFF_BASE * 2 ** (attempt - 1), BG_RETRY_BACKOFF_CAP)
            print(f"BG_LLM_RETRY: branch_id={result_container.get('branch_id')} attempt={attempt}/{BG_LLM_ATTEMPTS} "
                  f"wait={wait}s error={type(error).__name__}: {str(error)[:200]}")
            result_container["last_progress_at"] = time.time() + wait  # retry = still alive
            time.sleep(wait)
            if result_container.get("abandoned"):
                raise
        finally:
            result_container.pop("llm_inflight_since", None)
SLOW_STEP_DELAY = 10
ERROR_RECOVERY_TIME = 1 #after how long to retry when exception occurs
RETRY_BACKOFF_BASE = 5 #first wait after a retryable provider error (429/5xx/timeout); doubles per consecutive failure
RETRY_BACKOFF_CAP = 300 #maximum wait between retryable provider errors
RETRY_CAP_HITS_BEFORE_IDLE = 3 #consecutive capped waits before giving up and idling until new user input
BRANCH_IDLE_POLL_SECONDS = 1 #receive() poll interval while a background branch owns the current task
RETURN_VALUE_PRESERVE = 0
RETURN_VALUE_PRESERVE_MESSAGES = 10
DEFAULT_DELAY = 0 #default delay added irregard of whether in slow mode
AUTONOMOUS_CHECKIN_INTERVAL = 900 #min seconds between idle autonomous continuation turns (post_task_mode); receive() polling for real user input is never delayed by this
TEXT_ONLY_RETRY_CAP = 2 #text-only (no tool call) replies re-prompted before the turn is treated as nop; autonomous turns treat the first one as nop
MAX_TOKENS = 2524
INIT_WAIT = 10
MAX_TOOLS = 30
MAX_TOOL_DESCRIPTION_CHARS = 500
DYNAMIC_TIMEOUT = 5
MODEL = os.getenv("LLM_MODEL", "mlx-community/gemma-4-26b-a4b-it-4bit")
BASE_URL = os.getenv("BASE_URL", "http://192.168.64.1:2277/v1")
API_KEY = os.getenv("AI_API_KEY", "dummy")

# --------------------------------------------------------------------
# 1. Dynamic execution:
# --------------------------------------------------------------------
# Channel-contract hooks return JSON-native values (dict/list/bool) that the
# caller indexes (receive_request()["request_id"], resume_all()[0], ...);
# stringifying them broke multi-request binding (2026-10-01 staging e2e).
_STRUCTURED_RESULT_FUNCTIONS = frozenset({
    "receive_request", "resume_all", "active_request_ids", "close_active", "expire_stale"})

def dynamic_worker():
    path = Path(sys.argv[2])
    function = sys.argv[3]
    result_path = Path(sys.argv[4])
    payload_path = Path(sys.argv[5])
    try:
        payload = json.loads(payload_path.read_text())
        spec = importlib.util.spec_from_file_location("_dynamic_" + path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if function == "__description__":
            result = str(module.DESCRIPTION)
        elif function == "__has_attrs__":
            result = {name: callable(getattr(module, name, None)) for name in payload.get("args", [])}
        elif function == "__tool_metadata__":
            parameters = inspect.signature(module.run).parameters.values()
            description = str(module.DESCRIPTION)
            if len(description) > MAX_TOOL_DESCRIPTION_CHARS:
                description = description[:MAX_TOOL_DESCRIPTION_CHARS] + " [DESCRIPTION TRUNCATED]"
            result = {"description": description, "parameters": [parameter.name for parameter in parameters]}
        else:
            result = getattr(module, function)(*payload.get("args", []), **payload.get("kwargs", {}))
            if (function != "transform" and function not in _STRUCTURED_RESULT_FUNCTIONS
                    and result is not None):
                result = str(result)
        output = {"ok": True, "result": result}
    except BaseException as error:
        output = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    try:
        result_path.write_text(json.dumps(output, ensure_ascii=False))
    except BaseException as error:
        result_path.write_text(json.dumps({"ok": False, "error": f"Result serialization failed: {type(error).__name__}: {error}"}, ensure_ascii=False))

_tool_context = threading.local()  # per-thread request binding for tool subprocesses

def _tool_env():
    """Environment for tool subprocesses: adds ITER_REQUEST_IDS when this thread is bound."""
    ids = getattr(_tool_context, "request_ids", None)
    if ids is None:
        return None
    env = dict(os.environ)
    env["ITER_REQUEST_IDS"] = json.dumps(ids, sort_keys=True)
    return env

def invoke_dynamic(path, function, *args, **kwargs):
    result_fd, result_file = tempfile.mkstemp(prefix="iter-result-", suffix=".json")
    payload_fd, payload_file = tempfile.mkstemp(prefix="iter-payload-", suffix=".json")
    try:
        os.close(result_fd)
        os.close(payload_fd)
        Path(payload_file).write_text(json.dumps({"args": args, "kwargs": kwargs}, ensure_ascii=False))
        inherit_stdin = Path(path).stem == "terminal" and function == "receive"
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--invoke", str(Path(path).resolve()), function, result_file, payload_file],
            stdin=None if inherit_stdin else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=not inherit_stdin,
            close_fds=True,
            env=_tool_env(),
        )
        try:
            process.wait(timeout=DYNAMIC_TIMEOUT)
        except subprocess.TimeoutExpired:
            if inherit_stdin:
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            return {"ok": False, "error": f"TIMEOUT after {DYNAMIC_TIMEOUT}s"}
        try:
            return json.loads(Path(result_file).read_text())
        except Exception:
            return {"ok": False, "error": f"Dynamic process exited with code {process.returncode} without a valid result"}
    finally:
        for file in (result_file, payload_file):
            try:
                os.unlink(file)
            except FileNotFoundError:
                pass

if len(sys.argv) > 1 and sys.argv[1] == "--invoke":
    dynamic_worker()
    sys.exit(0)

# --------------------------------------------------------------------
# 2. Runtime helpers:
# --------------------------------------------------------------------
def get_current_time():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

_CHANNEL_PROBE_NAMES = ("receive_request", "resume_all", "active_request_ids", "close_active", "expire_stale")
_channel_probe_cache = {}  # resolved path -> (mtime_ns, size, {name: bool})

def _channel_has(path, name):
    """True if the channel module actually exposes callable *name* once imported.

    A text search is not enough: a channel can mention a hook in a guarded
    `try: from ... import hook / except ImportError: pass` block while the
    backing library lacks it (19:01 Sep 30 deploy outage). Probe with hasattr
    in a subprocess, cached per file mtime/size; probe failure -> False."""
    try:
        stat = Path(path).stat()
    except OSError:
        return False
    key = str(Path(path).resolve())
    cached = _channel_probe_cache.get(key)
    if cached is None or cached[:2] != (stat.st_mtime_ns, stat.st_size):
        names = tuple(dict.fromkeys(_CHANNEL_PROBE_NAMES + (name,)))
        result = invoke_dynamic(path, "__has_attrs__", *names)
        found = result.get("result") if result.get("ok") and isinstance(result.get("result"), dict) else {}
        cached = (stat.st_mtime_ns, stat.st_size, {n: bool(found.get(n)) for n in names})
        _channel_probe_cache[key] = cached
    if name not in cached[2]:
        _channel_probe_cache.pop(key, None)
        return _channel_has(path, name)
    return cached[2][name]

def _foreground_busy(path):
    """True while the foreground's bound request on this channel is still open."""
    rid = _foreground_request_ids.get(path.stem)
    if rid is None:
        return False
    result = invoke_dynamic(path, "active_request_ids")
    if result.get("ok") and rid in (result.get("result") or []):
        return True
    _foreground_request_ids.pop(path.stem, None)  # answered/closed: foreground is free
    return False

def _resume_bound(path):
    """Startup recovery with binding: resume the oldest open request, close the rest."""
    result = invoke_dynamic(path, "resume_all")
    if not result["ok"]:
        return f"[CHANNEL RECOVERY ERROR in {path}: {result['error']}. Repair {path} if needed.]"
    claims = result["result"] or []
    if not claims:
        return ""
    for extra in claims[1:]:
        closed = invoke_dynamic(path, "close_active", request_id=extra["request_id"])
        print(f"RESUME_EXTRA_REQUEST_CLOSED: channel={path.stem} request={extra['request_id'][:12]} ok={closed.get('ok')}")
    _foreground_request_ids[path.stem] = claims[0]["request_id"]
    return "[" + path.stem + "] " + str(claims[0]["prompt"])

def receive():
    events = []
    paths = [path for path in sorted(Path("channels").glob("*.py")) if not path.name.startswith("_")]
    for path in paths:
        try:
            if ITER_REQUEST_BINDING and _channel_has(path, "receive_request"):
                if _foreground_busy(path):
                    continue  # one foreground request at a time; new input waits in the inbox
                result = invoke_dynamic(path, "receive_request")
                if not result["ok"]:
                    raise RuntimeError(result["error"])
                claimed = result["result"]
                if claimed:
                    _foreground_request_ids[path.stem] = claimed["request_id"]
                    events.append("[" + path.stem + "] " + str(claimed["prompt"]))
                continue
            result = invoke_dynamic(path, "receive")
            if not result["ok"]:
                raise RuntimeError(result["error"])
            event = result["result"]
            if event:
                events.append("[" + path.stem + "] " + str(event))
        except Exception as error:
            events.append(f"[CHANNEL ERROR in {path}: {type(error).__name__}: {error}. Repair {path} if needed.]")
    return "\n".join(events)

def resume_claimed():
    """Resume channel claims once at process startup.

    Channels without a recovery hook are ignored. A channel that implements
    ``resume`` remains responsible for validating that exactly one durable
    claim exists and for failing closed on ambiguous state.
    """
    events = []
    paths = [path for path in sorted(Path("channels").glob("*.py")) if not path.name.startswith("_")]
    for path in paths:
        if ITER_REQUEST_BINDING and _channel_has(path, "resume_all"):
            event = _resume_bound(path)
            if event:
                events.append(event)
            continue
        result = invoke_dynamic(path, "resume")
        if not result["ok"]:
            if "AttributeError" in result["error"] and "resume" in result["error"]:
                continue
            if "exactly one active request is required for resume" in result["error"]:
                continue
            events.append(f"[CHANNEL RECOVERY ERROR in {path}: {result['error']}. Repair {path} if needed.]")
            continue
        event = result["result"]
        if event:
            events.append("[" + path.stem + "] " + str(event))
    return "\n".join(events)

ITER_REQUEST_TIMEOUT = float(os.getenv("ITER_REQUEST_TIMEOUT", "3600"))

def close_unfinished_requests():
    """Turn-end safety net (multi-send channels only):
    1. expire_stale: close any request open longer than ITER_REQUEST_TIMEOUT,
       even while a background branch runs (a hung branch must not hold it forever).
    2. close_active: close a request left open without a final send.
       Skipped while a background branch is active, since the branch may own it."""
    branch_active = ITER_CONCURRENCY_ENABLED and background_branch_active()
    for channel_path in sorted(Path("channels").glob("*.py")):
        if not channel_path.is_file():
            continue
        if _channel_has(channel_path, "expire_stale") and ITER_REQUEST_TIMEOUT > 0:
            result = invoke_dynamic(channel_path, "expire_stale", ITER_REQUEST_TIMEOUT)
            if result.get("ok") and result.get("result"):
                print(f"STALE_REQUEST_EXPIRED: channel={channel_path.stem}")
                if not ITER_REQUEST_BINDING:
                    continue
            elif not result.get("ok"):
                print(f"STALE_REQUEST_EXPIRE_FAILED: channel={channel_path.stem} error={result.get('error')}")
        if not _channel_has(channel_path, "close_active"):
            continue
        if ITER_REQUEST_BINDING and _channel_has(channel_path, "receive_request"):
            # Close only the foreground's own request; branches close theirs when they finish.
            rid = _foreground_request_ids.pop(channel_path.stem, None)
            if rid is None:
                continue
            result = invoke_dynamic(channel_path, "close_active", request_id=rid)
        elif branch_active:
            continue
        else:
            result = invoke_dynamic(channel_path, "close_active")
        if result.get("ok") and result.get("result"):
            print(f"UNFINISHED_REQUEST_CLOSED: channel={channel_path.stem}")
        elif not result.get("ok"):
            print(f"UNFINISHED_REQUEST_CLOSE_FAILED: channel={channel_path.stem} error={result.get('error')}")

def slow_wait_for_input():
    for second in range(SLOW_STEP_DELAY):
        time.sleep(1)
        event_append = receive()
        if event_append:
            return event_append
    return ""

def save_experience(experience):
    with open("experience.tmp", "w", encoding="utf-8") as file:
        json.dump(experience, file, ensure_ascii=False, indent=2)
    os.replace("experience.tmp", "experience.json")

# --- M1 Step 1.1: Tier-1 mechanical checkpoint extraction ---
def extract_tier1_checkpoint(experience, autonomous_steps):
    """Purely mechanical extraction of checkpoint data from the experience list.

    No LLM calls. Returns a dict with: checkpoint_type, session_id, step_count,
    timestamps, prompt_hash, and a snapshot of the last N tool calls (name +
    200-char truncated output).
    """
    # Map tool_call_id -> tool name from assistant messages
    tool_call_names = {}
    for entry in experience:
        if entry.get("role") == "assistant" and entry.get("tool_calls"):
            for tc in entry["tool_calls"]:
                if isinstance(tc, dict):
                    tool_call_names[tc.get("id")] = tc.get("function", {}).get("name", "?")

    # Collect last N tool-output entries
    tool_entries = [e for e in experience if e.get("role") == "tool"]
    recent_tools = tool_entries[-CHECKPOINT_TOOL_SNAPSHOT:]

    snapshot = []
    for entry in recent_tools:
        tc_id = entry.get("tool_call_id", "?")
        name = tool_call_names.get(tc_id, "?")
        content = entry.get("content", "")
        truncated = content[:CHECKPOINT_OUTPUT_CHARS]
        snapshot.append({
            "tool_call_id": tc_id,
            "tool_name": name,
            "output_truncated": truncated,
        })

    # Prompt hash for provenance
    try:
        prompt_text = Path("prompt.txt").read_text(encoding="utf-8", errors="replace")
        prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]
    except Exception:
        prompt_hash = "unknown"

    return {
        "checkpoint_type": "tier1_mechanical",
        "session_id": SESSION_ID,
        "step_count": autonomous_steps,
        "timestamp": get_current_time(),
        "timestamp_iso": datetime.datetime.now().isoformat(),
        "prompt_hash": prompt_hash,
        "tool_snapshot": snapshot,
    }

# --- M1 Step 1.2: Atomic checkpoint file write (temp+rename) ---
def write_checkpoint_file(checkpoint_data):
    """Atomically write checkpoint JSON to checkpoints/<session>/<ts>.json.

    Uses temp-file + os.replace for atomicity. Returns the Path on success,
    or None on failure (never raises — file write failure must not crash the loop).
    Per v4 write-ordering: file is written BEFORE any send attempt.
    """
    try:
        session_dir = CHECKPOINT_DIR / checkpoint_data.get("session_id", "unknown")
        session_dir.mkdir(parents=True, exist_ok=True)
        # Filename-safe timestamp: 20260904T215400
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        dest = session_dir / f"{ts}.json"
        # Avoid collision if two checkpoints land in the same second
        if dest.exists():
            dest = session_dir / f"{ts}_{uuid.uuid4().hex[:4]}.json"
        tmp = session_dir / f"{ts}.json.tmp"
        tmp.write_text(json.dumps(checkpoint_data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(dest))
        return dest
    except Exception as error:
        print(f"CHECKPOINT_FILE_WRITE_FAILED: {type(error).__name__}: {error}")
        # Clean up temp file if it exists
        try:
            if 'tmp' in dir() and tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return None

# --- M1 Step 1.3: Human-readable checkpoint via normal send path ---
def format_checkpoint_message(checkpoint_data, checkpoint_path):
    """Format a human-readable checkpoint summary from Tier-1 data.

    Returns a concise string suitable for sending through the channel.
    """
    if checkpoint_data.get("stop_reason"):
        lines = [
            f"[PAUSED] I stopped working on this at step {checkpoint_data.get('step_count', '?')} because "
            f"{checkpoint_data['stop_reason']}, to avoid runaway usage. My state is saved; reply "
            "'continue' (or add instructions) and I will resume.",
        ]
    else:
        lines = [
            f"[CHECKPOINT] Turn budget exhausted after {checkpoint_data.get('step_count', '?')} autonomous steps without a send.",
        ]
    snapshot = checkpoint_data.get("tool_snapshot", [])
    if snapshot:
        lines.append(f"Last {len(snapshot)} tool calls:")
        for i, entry in enumerate(snapshot, 1):
            name = entry.get("tool_name", "?")
            output = entry.get("output_truncated", "")
            lines.append(f"  {i}. {name}: {output}")
    else:
        lines.append("(no tool calls in snapshot)")
    if checkpoint_path:
        lines.append(f"Checkpoint file: {checkpoint_path}")
    else:
        lines.append("(checkpoint file write failed — this is the human-readable fallback)")
    lines.append(f"Session: {checkpoint_data.get('session_id', '?')}")
    return "\n".join(lines)

def send_checkpoint_message(checkpoint_data, checkpoint_path):
    """Send a human-readable checkpoint summary via the normal send tool.

    Best-effort: never raises. Returns True on success, False on failure.
    Per v4 write-ordering this is called AFTER the file write.
    """
    try:
        message = format_checkpoint_message(checkpoint_data, checkpoint_path)
        result = invoke_dynamic(Path("tools/send.py"), "run",
                                channel=CHECKPOINT_CHANNEL, content=message)
        if not result["ok"]:
            print(f"CHECKPOINT_SEND_FAILED: {result['error']}")
            return False
        send_result = result["result"]
        if send_result != "SUCCESS":
            print(f"CHECKPOINT_SEND_NONSUCCESS: {send_result}")
            return False
        print(f"CHECKPOINT_SENT via channel={CHECKPOINT_CHANNEL}")
        return True
    except Exception as error:
        print(f"CHECKPOINT_SEND_EXCEPTION: {type(error).__name__}: {error}")
        return False

RETRYABLE_PROVIDER_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
QUOTA_EXHAUSTED_PATTERN = re.compile(
    r"usage limit|next reset in|insufficient_quota|quota exceeded|exceeded your current quota",
    re.IGNORECASE)

def retryable_provider_error(error):
    """True for transient provider failures (rate limit, 5xx, timeout, disconnect) that deserve backoff."""
    for candidate in (error, getattr(error, "response", None)):
        status = getattr(candidate, "status_code", None)
        if type(status) is int and status in RETRYABLE_PROVIDER_STATUS_CODES:
            return True
    if isinstance(error, (openai.APITimeoutError, openai.APIConnectionError)):
        return True
    text = str(error)
    codes = "|".join(str(code) for code in sorted(RETRYABLE_PROVIDER_STATUS_CODES))
    if re.search(rf"\bError code:\s*({codes})\b", text):
        return True
    return bool(re.search(r"RateLimitError|APITimeoutError|APIConnectionError|InternalServerError|timed out|ClientDisconnect",
                          text))

def provider_quota_exhausted(error):
    """True when the provider reports a long-lived quota/usage limit rather than a short rate limit."""
    return bool(QUOTA_EXHAUSTED_PATTERN.search(str(error)))

def retry_after_seconds(error):
    """Return the provider's Retry-After hint in seconds, if present."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None

def retry_backoff_delay(consecutive_failures, retry_after=None, jitter=None):
    """Exponential backoff: RETRY_BACKOFF_BASE * 2^(n-1), capped, plus up to 10% jitter; honours Retry-After."""
    exponent = max(consecutive_failures - 1, 0)
    delay = min(RETRY_BACKOFF_BASE * (2 ** min(exponent, 16)), RETRY_BACKOFF_CAP)
    if retry_after is not None:
        delay = max(delay, min(retry_after, 3600))
    if jitter is None:
        jitter = int.from_bytes(os.urandom(2), "big") / 65535
    return delay + delay * 0.1 * jitter

def backoff_wait(seconds):
    """Sleep `seconds` while polling receive() each second; return any inbound events (never dropped)."""
    events = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(min(1, max(deadline - time.time(), 0)))
        event_append = receive()
        if event_append:
            events.append(event_append)
    return "\n".join(events)

def send_provider_exhausted_notice(error):
    """Send one bounded, non-secret notice that the provider is unavailable and Iter is idling."""
    content = (
        "ProtoCosmo2 paused this request: its model provider keeps rejecting calls "
        "(rate limit or exhausted usage quota). It will not retry automatically; "
        "send a new message to try again."
    )
    result = invoke_dynamic(Path("tools/send.py"), "run",
                            channel=CHECKPOINT_CHANNEL, content=content)
    if not result["ok"] or result["result"] != "SUCCESS":
        print(f"PROVIDER_EXHAUSTED_NOTICE_SEND_FAILED: {result}")
        return
    print(f"PROVIDER_EXHAUSTED_NOTICE_SENT: {type(error).__name__}")

TEXT_ONLY_FALLBACK_MAX_CHARS = 3500
_PSEUDO_SEND_RE = re.compile(r"name=send\s+arguments=(?=\{)")

def unwrap_pseudo_send(text):
    """If *text* is a send tool call written out as plain text, return its content; else *text*."""
    match = _PSEUDO_SEND_RE.search(text or "")
    if not match:
        return text
    try:
        arguments, _end = json.JSONDecoder().raw_decode(text, match.end())
    except ValueError:
        return text
    content = arguments.get("content") if isinstance(arguments, dict) else None
    return content if isinstance(content, str) and content.strip() else text

def send_text_only_fallback(content):
    """Post a text-only reply that would otherwise be dropped. Best-effort; returns True on success."""
    text = unwrap_pseudo_send((content or "").strip()).strip()
    if not text:
        print("TEXT_ONLY_FALLBACK_SKIPPED: empty")
        return False
    if len(text) > TEXT_ONLY_FALLBACK_MAX_CHARS:
        text = text[:TEXT_ONLY_FALLBACK_MAX_CHARS] + " [...truncated]"
    try:
        result = invoke_dynamic(Path("tools/send.py"), "run", channel=CHECKPOINT_CHANNEL, content=text)
    except Exception as error:
        print(f"TEXT_ONLY_FALLBACK_EXCEPTION: {type(error).__name__}: {error}")
        return False
    if not result["ok"] or result["result"] != "SUCCESS":
        print(f"TEXT_ONLY_FALLBACK_SEND_FAILED: {result}")
        return False
    print(f"TEXT_ONLY_FALLBACK_SENT: chars={len(text)}")
    return True

# BEGIN PROGRESS_NOT_PAUSE helpers
def step_signature(tool_calls):
    """Order-insensitive fingerprint of one step's tool names + arguments."""
    calls = []
    for call in tool_calls or []:
        try:
            arguments = json.dumps(json.loads(call.function.arguments), sort_keys=True, ensure_ascii=False)
        except Exception:
            arguments = str(call.function.arguments)
        calls.append(call.function.name + "\x00" + arguments)
    return hashlib.sha256("\x01".join(sorted(calls)).encode("utf-8", errors="replace")).hexdigest()

def _tally_text(tally):
    """Mechanical tool-name counts only (no arguments/outputs, so nothing sensitive leaks)."""
    if not tally:
        return "no tool calls yet"
    return ", ".join(f"{name} x{count}" for name, count in sorted(tally.items(), key=lambda item: (-item[1], item[0])))

class StepGuard:
    """Per-request step accounting for the foreground turn or one background branch.

    record_step() is called once per model step (after its tool calls ran) and
    returns (action, text): ("stop", reason) for a runaway backstop, ("nudge", note)
    or ("warn", note) to show the model on its next step, ("fallback", content) for
    Iter to post itself as a non-closing progress note, or (None, None).
    """
    paused_by_backstop = False  # class-wide: the next inbound event gets a resume NOTE

    def __init__(self, has_request=True, steps=0):
        self.has_request = has_request  # an open user request this work answers
        self.steps = steps
        self.since_note = 0
        self.last_note_at = time.time()
        self.nudge_step = None
        self.signatures = []
        self.seen = set()
        self.stale = 0
        self.tally = {}
        self.answered = False  # a final send succeeded: no more interim notes

    def fork(self):
        return copy.deepcopy(self)

    def record_step(self, tool_calls, results):
        """results: [(tool_name, raw_arguments, return_text)] for this step."""
        self.steps += 1
        noted = False
        digests = []
        for name, raw_arguments, ret in results:
            self.tally[name] = self.tally.get(name, 0) + 1
            ret = str(ret)
            if name == "send" and ret.startswith("SUCCESS"):
                noted = True
                if not ret.startswith("SUCCESS (progress"):
                    self.answered = True
            digests.append(hashlib.sha256((str(name) + "\x00" + str(raw_arguments) + "\x00" + ret)
                                          .encode("utf-8", errors="replace")).hexdigest())
        if noted:
            self.since_note, self.last_note_at, self.nudge_step = 0, time.time(), None
        else:
            self.since_note += 1
        self.signatures = (self.signatures + [step_signature(tool_calls)])[-REPEAT_STEPS:]
        new_results = [digest for digest in digests if digest not in self.seen]
        self.seen.update(digests)
        self.stale = 0 if (new_results or not digests) else self.stale + 1
        if self.steps >= HARD_STEP_CEILING:
            return "stop", f"it reached the per-request step ceiling ({HARD_STEP_CEILING} steps)"
        if len(self.signatures) >= REPEAT_STEPS and len(set(self.signatures)) == 1:
            return "stop", (f"its last {REPEAT_STEPS} steps repeated the same tool call with the same "
                            "arguments (possible loop)")
        if self.stale >= STALL_STEPS:
            return "stop", f"its last {STALL_STEPS} steps produced no new results (possible loop)"
        if not self.has_request or self.answered:
            return None, None
        if self.nudge_step is not None:
            if self.steps - self.nudge_step >= PROGRESS_FALLBACK_STEPS:
                self.nudge_step = None
                self.since_note, self.last_note_at = 0, time.time()
                return "fallback", (f"Still working on this (step {self.steps}), not paused; I will keep going. "
                                    f"Actions so far: {_tally_text(self.tally)}.")
            return None, None
        if ((self.steps % PROGRESS_EVERY == 0 and self.since_note >= 3)
                or time.time() - self.last_note_at >= PROGRESS_MAX_SILENT_SECONDS):
            self.nudge_step = self.steps
            return "nudge", (
                f"[PROGRESS CHECKPOINT: you are at step {self.steps} of this request (runaway ceiling "
                f"{HARD_STEP_CEILING}) and have not updated the user recently. In your next step, call send with "
                "final=false and a concise interim note (done / doing next / blockers; no secrets), then KEEP "
                "WORKING on the task. Do not stop, do not ask the user to type 'continue', and use final=true "
                "only for your final answer.]")
        if self.steps == HARD_STEP_CEILING - 2 and self.since_note >= 3:
            return "warn", (
                f"[STEP CEILING WARNING: you are at step {self.steps} of the {HARD_STEP_CEILING}-step runaway "
                "ceiling for this request. Within your next step, call send (final=true) with a concise summary "
                "(done / remaining / current state; no secrets) and pin the key state so you can resume. The "
                "user can reply 'continue' to resume.]")
        return None, None

def send_progress_note(content):
    """Post Iter's own interim note as a NON-closing progress message (send final=false).

    No model call.  Bound to the calling thread's request (ITER_REQUEST_IDS).
    Returns True when the channel accepted it.
    """
    try:
        result = invoke_dynamic(Path("tools/send.py"), "run", channel=CHECKPOINT_CHANNEL, content=content, final=False)
    except Exception as error:
        print(f"PROGRESS_NOTE_EXCEPTION: {type(error).__name__}: {error}")
        return False
    if not result.get("ok") or not str(result.get("result")).startswith("SUCCESS"):
        print(f"PROGRESS_NOTE_SEND_FAILED: {result}")
        return False
    return True

_foreground_step_guard = None  # main loop's current StepGuard; a promoted branch forks it
# END PROGRESS_NOT_PAUSE helpers

# --- Thread visibility: each branch (and the foreground) sees what the others are doing ---
# Background branches used to be blind to each other: two threads could answer the same
# topic twice, and a follow-up message could not reach the thread it belonged to.
SIBLING_SEND_WINDOW = int(os.getenv("ITER_SIBLING_SEND_WINDOW", "1800"))  # seconds of recent sends to show
SIBLING_SNIPPET_CHARS = 300
_sibling_lock = threading.Lock()
_branch_activity = {}   # branch_id -> {"request", "started", "done_at", "inbox"}
_recent_sends = []      # [(time, origin, snippet)], newest last, bounded
_last_inbound = None    # (time, snippet) of the newest user message

def _snippet(text, limit=SIBLING_SNIPPET_CHARS):
    """Collapse whitespace and keep the tail (where the actual user words are) or head of sends."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"

def _inbound_snippet(text, limit=SIBLING_SNIPPET_CHARS):
    """Keep the end of an inbound event: channel context comes first, the new message last."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else "…" + text[-limit:]

def _note_send(origin, content):
    """Record a successful send so other threads can see it."""
    with _sibling_lock:
        _recent_sends.append((time.time(), origin, _snippet(content)))
        del _recent_sends[:-20]

def _note_inbound(event_text):
    """Main thread: a new user message arrived; queue it for every running branch."""
    global _last_inbound
    snippet = _inbound_snippet(event_text)
    with _sibling_lock:
        _last_inbound = (time.time(), snippet)
        for activity in _branch_activity.values():
            if activity["done_at"] is None:
                activity["inbox"].append((time.time(), snippet))

def _register_branch_activity(branch_id):
    """Record what a newly promoted branch is working on (the newest user message)."""
    with _sibling_lock:
        request = _last_inbound[1] if _last_inbound else "(autonomous work, no user message)"
        _branch_activity[branch_id] = {"request": request, "started": time.time(),
                                       "done_at": None, "inbox": []}
        for stale in [b for b, a in _branch_activity.items()
                      if a["done_at"] and time.time() - a["done_at"] > SIBLING_SEND_WINDOW]:
            del _branch_activity[stale]

def _mark_branch_activity_done(branch_id):
    with _sibling_lock:
        if branch_id in _branch_activity and _branch_activity[branch_id]["done_at"] is None:
            _branch_activity[branch_id]["done_at"] = time.time()

def take_branch_inbox(branch_id):
    """Branch thread: new user messages that arrived while it was running (drained), or ''."""
    with _sibling_lock:
        activity = _branch_activity.get(branch_id)
        items = activity["inbox"] if activity else []
        if activity:
            activity["inbox"] = []
    if not items:
        return ""
    lines = [f"- {time.strftime('%H:%M:%S', time.localtime(t))}: {s}" for t, s in items]
    return ("[NEW USER MESSAGE(S) ARRIVED WHILE YOU WORKED. Another thread opens a request for each one "
            "and answers it, but if one is a follow-up to YOUR task (adds detail, corrects or redirects it), "
            "take it into account in your answer:]\n" + "\n".join(lines))

def sibling_view(self_id):
    """Text describing other threads' tasks and recent sends, or '' when there is nothing to show.

    self_id is a branch id, or None for the foreground turn.
    """
    now = time.time()
    with _sibling_lock:
        others = [(b, dict(a)) for b, a in _branch_activity.items() if b != self_id and a["done_at"] is None]
        sends = [(t, o, s) for t, o, s in _recent_sends if o != (self_id or "foreground")
                 and now - t <= SIBLING_SEND_WINDOW]
    if not others and not sends:
        return ""
    lines = ["[OTHER THREADS (visibility only; you cannot message them). Do not repeat an answer another "
             "thread already sent; if your task is a follow-up to one of theirs, build on it."]
    for branch_id, activity in others:
        lines.append(f"- running {branch_id} ({int(now - activity['started'])}s): {activity['request']}")
    for t, origin, snippet in sends[-8:]:
        lines.append(f"- sent by {origin} at {time.strftime('%H:%M:%S', time.localtime(t))}: {snippet}")
    return "\n".join(lines)

def background_branch_active():
    """True while a promoted background branch still owns the current task."""
    with _branch_lock:
        return bool(_active_branches)

def background_branch_count():
    """Number of promoted background branches currently running."""
    with _branch_lock:
        return len(_active_branches)

_branch_error = None  # last provider error raised inside a background branch, for main-loop backoff

def record_branch_error(error):
    """Hand a background-branch provider error to the main loop (terminal breaker / backoff)."""
    global _branch_error
    with _branch_lock:
        _branch_error = error

def pop_branch_error():
    """Return and clear the pending background-branch error, if any."""
    global _branch_error
    with _branch_lock:
        error, _branch_error = _branch_error, None
    return error

# --- M2 Step 2.1: Threaded LLM call wrapper with deadline T ---
# Background branch state (populated when ITER_CONCURRENCY_ENABLED)
_branch_lock = threading.Lock()
_active_branches = {}  # branch_id -> BranchState; at most ITER_MAX_BACKGROUND_BRANCHES

class BranchState:
    """State for a promoted background LLM call branch (R11: deep copy, R12: separate client)."""
    def __init__(self, branch_id, branch_client, branch_messages, thread, result_container, request_ids=None):
        """Initialize branch state for a background LLM call."""
        self.branch_id = branch_id
        self.branch_client = branch_client
        self.branch_messages = branch_messages  # deep copy per R11
        self.thread = thread
        self.result_container = result_container
        self.created_at = time.time()
        self.completed = False
        self.request_ids = dict(request_ids or {})  # channel -> request this branch answers

def _release_branch(branch_id, result_container, reason):
    """Free a branch's slot and ask the main thread to close any request it left open."""
    result_container["finished"] = True
    _mark_branch_activity_done(branch_id)
    with _branch_lock:
        branch = _active_branches.pop(branch_id, None)
    if branch is None:
        return False
    if branch.request_ids:
        _merge_queue.put({"_branch_done": True, "branch": branch_id, "reason": reason,
                          "request_ids": dict(branch.request_ids)})
    return True

def _call_bound(request_ids, function, *args):
    """Run *function* with tool subprocesses bound to *request_ids* (main-thread sends for a branch)."""
    if request_ids is None or not ITER_REQUEST_BINDING:
        return function(*args)
    previous = getattr(_tool_context, "request_ids", None)
    _tool_context.request_ids = dict(request_ids)
    try:
        return function(*args)
    finally:
        _tool_context.request_ids = previous

def _close_branch_requests(done):
    """Main thread: close requests a finished branch left unanswered (safety net)."""
    for channel, rid in sorted((done.get("request_ids") or {}).items()):
        if _foreground_request_ids.get(channel) == rid:
            continue  # the foreground owns it again
        path = Path("channels") / (channel + ".py")
        result = invoke_dynamic(path, "close_active", request_id=rid)
        if result.get("ok") and result.get("result"):
            print(f"BRANCH_REQUEST_CLOSED: branch_id={done.get('branch')} reason={done.get('reason')} request={rid[:12]}")
        elif not result.get("ok"):
            print(f"BRANCH_REQUEST_CLOSE_FAILED: branch_id={done.get('branch')} request={rid[:12]} error={result.get('error')}")

def _promotion_lock(result_container):
    """The per-call lock that orders "call finished" against "call promoted" (dict.setdefault is atomic)."""
    return result_container.setdefault("promotion_lock", threading.Lock())

def _bg_llm_thread_target(llm_client, model, messages, tools, tool_choice, max_tokens, extra_body, result_container):
    """Thread target for background LLM call; stores result in result_container.

    On exception (R14): if the call was promoted to a background branch
    (branch_id available in result_container), pushes an error marker
    (type + bounded message) onto _merge_queue and frees the branch slot.
    The main loop's drain_merge_queue() surfaces the error as a normal
    experience entry (R6 single-writer: only the main thread appends to
    experience, so the marker goes through the queue, not directly).
    """
    try:
        response = _bg_llm_create(
            llm_client, result_container, model=model, messages=messages, tools=tools,
            tool_choice=tool_choice, max_tokens=max_tokens, extra_body=extra_body,
        )
        # Promotion race (2026-10-12): the outcome and branch_id are read/written under
        # promotion_lock, so the call either finished before promotion (foreground keeps it)
        # or the branch is fully set up (client + messages) before this thread sees branch_id.
        with _promotion_lock(result_container):
            result_container["ok"] = True
            result_container["response"] = response
            result_container["last_progress_at"] = time.time()  # R13: progress resets idle deadline
            # M3 Step 3.5: if promoted, run the branch mini-loop (R20: step budget + checkpoint queuing)
            branch_id = result_container.get("branch_id")
        if branch_id and result_container.get("abandoned"):
            print(f"BACKGROUND_ABANDONED_RESULT_DROPPED: branch_id={branch_id}")
            return
        if branch_id:
            branch_client = result_container.get("branch_client")
            branch_messages = result_container.get("branch_messages")
            if branch_client is not None and branch_messages is not None:
                _bg_branch_mini_loop(branch_id, branch_client, branch_messages, response, result_container)
    except Exception as e:
        with _promotion_lock(result_container):
            result_container["ok"] = False
            result_container["error"] = f"{type(e).__name__}: {e}"
            result_container["exception"] = e  # K9: the caller re-raises the original (status_code, response headers)
            # R14: push error marker to merge queue and free branch slot
            branch_id = result_container.get("branch_id")
        if branch_id:
            error_marker = {
                "role": "system",
                "content": (
                    f"[BACKGROUND_BRANCH_ERROR] branch_id={branch_id} "
                    f"error={type(e).__name__}: {str(e)[:500]}"
                ),
                "branch": branch_id,
                "error": True,
                "error_type": type(e).__name__,
                "error_message": str(e)[:500],
            }
            _merge_queue.put(error_marker)
            record_branch_error(e)
            # Free the branch slot and hand its request back to the main thread
            _release_branch(branch_id, result_container, "error")
            print(f"BACKGROUND_ERROR: branch_id={branch_id} error={type(e).__name__}: {e}")

# --- M3 Step 3.5: Background branch mini-loop (R20: branch step budget + checkpoint queuing) ---
def _bg_branch_mini_loop(branch_id, branch_client, branch_messages, initial_response, result_container):
    """Background branch mini-loop after promotion (R20: branch step budget + checkpoint queuing).

    Runs up to BRANCH_STEP_BUDGET follow-up steps in the background thread:
    - Pushes assistant + tool entries to _merge_queue (tagged with branch id).
    - Makes follow-up LLM calls with branch_client on branch_messages (deep copy, R11).
    - On step-budget exhaustion: queues Tier-1 checkpoint data to _merge_queue
      (R20: never writes files directly — the main thread handles file write + send).
    - On exception in follow-up LLM call: pushes error marker (R14) and frees slot.
    - On normal completion (no more tool calls): frees slot.

    Flag-off: never called; threaded_llm_call is behind ITER_CONCURRENCY_ENABLED.
    """
    if result_container.get("request_ids") is not None:
        _tool_context.request_ids = dict(result_container["request_ids"])  # sends answer this branch's request
    response = initial_response
    branch_steps = 0
    branch_sent = False  # a final send succeeded, so the branch's request already has an answer
    guard = result_container.get("step_guard") or StepGuard()  # PROGRESS_NOT_PAUSE
    stop_reason, branch_note = None, None

    # Load tools once at the start (stateless subprocesses; coherence model: snapshot)
    inops, _omitted, _tool_errors = load_tools()
    tools = native_tools(inops)

    while branch_steps < BRANCH_STEP_BUDGET:
        if result_container.get("abandoned"):
            # Deadline abandonment must actually stop work, not just discard results.
            print(f"BRANCH_STOPPED_AFTER_ABANDON: branch_id={branch_id} steps={branch_steps}")
            return
        message = response.choices[0].message

        # Build assistant entry for merge queue (tagged) and branch_messages (untagged)
        assistant_entry = {key: value for key, value in message.model_dump(exclude_none=True).items()
                          if KEEP_REASONING_IN_EPISODE or key not in ("reasoning", "reasoning_details", "reasoning_content")}
        _merge_queue.put({**assistant_entry, "branch": branch_id})
        branch_messages.append(assistant_entry)

        if not message.tool_calls:
            # Branch is done. A text-only ending with no send would leave the request
            # unanswered (the safety net then posts the "stopped working" note): deliver it.
            if not branch_sent and not result_container.get("abandoned"):
                if send_text_only_fallback(message.content):
                    print(f"BRANCH_TEXT_ONLY_FALLBACK_SENT: branch_id={branch_id}")
            break

        # Execute tool calls (stateless subprocesses)
        step_results = []
        for tool_call in message.tool_calls[:MAX_TOOL_CALLS]:
            tool_name = tool_call.function.name
            try:
                tool_arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as error:
                tool_arguments = tool_call.function.arguments
                ret = f"Invalid tool arguments from model: {error}"
            else:
                try:
                    if tool_name not in inops:
                        ret = f"Unknown tool: {tool_name!r}"
                    elif not isinstance(tool_arguments, dict):
                        ret = "Tool arguments must be a JSON object"
                    else:
                        result = invoke_dynamic(inops[tool_name][0], "run", **tool_arguments)
                        ret = result["result"] if result["ok"] else f"Tool execution failed: {result['error']}"
                        if tool_name == "send" and result["ok"] and str(result["result"]).startswith("SUCCESS"):
                            if not str(result["result"]).startswith("SUCCESS (progress"):
                                branch_sent = True  # a progress note does not answer the request
                            _note_send(branch_id, tool_arguments.get("content"))
                except Exception as error:
                    ret = f"Tool execution failed: {type(error).__name__}: {error}"
            ret = str(ret)
            step_results.append((tool_name, tool_call.function.arguments, ret))
            if len(ret) > MAX_TOOL_OUTPUT_CHARS:
                ret = ret[:MAX_TOOL_OUTPUT_CHARS] + " [TRUNCATED]"
            tool_content = "Step " + get_current_time() + ": " + ret
            _merge_queue.put({"role": "tool", "tool_call_id": tool_call.id, "content": tool_content, "branch": branch_id})
            branch_messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_content})
            result_container["last_progress_at"] = time.time()  # R13: tool result = progress

        branch_steps += 1
        # PROGRESS_NOT_PAUSE: interim notes, not a pause; only runaway backstops stop the branch.
        guard_action, guard_text = guard.record_step(message.tool_calls[:MAX_TOOL_CALLS], step_results)
        if guard_action == "stop":
            stop_reason = guard_text
            print(f"BRANCH_BACKSTOP: branch_id={branch_id} steps={guard.steps} reason={guard_text}")
            break
        if guard_action in ("nudge", "warn"):
            branch_note = guard_text
            print(f"BRANCH_PROGRESS_{guard_action.upper()}: branch_id={branch_id} step={guard.steps}")
        elif guard_action == "fallback":
            delivered = send_progress_note(guard_text)
            print(f"BRANCH_PROGRESS_FALLBACK: branch_id={branch_id} step={guard.steps} delivered={delivered}")
        if branch_steps >= BRANCH_STEP_BUDGET:
            break

        # Thread visibility: follow-ups that arrived meanwhile persist in the branch; the
        # sibling summary is rebuilt fresh for every call.
        inbox = take_branch_inbox(branch_id)
        if inbox:
            branch_messages.append({"role": "user", "content": inbox})
            print(f"BRANCH_INBOX_DELIVERED: branch_id={branch_id}")
        if branch_note:
            branch_messages.append({"role": "user", "content": "Step " + get_current_time() + ": " + branch_note})
            branch_note = None
        view = sibling_view(branch_id)
        call_messages = branch_messages + ([{"role": "user", "content": view}] if view else [])

        # Make follow-up LLM call with branch_client (R12: separate client)
        try:
            response = _bg_llm_create(
                branch_client, result_container, model=MODEL, messages=call_messages, tools=tools,
                tool_choice="required", max_tokens=MAX_TOKENS,
                extra_body={"enable_thinking": True},
            )
            result_container["last_progress_at"] = time.time()  # R13: LLM step = progress
        except Exception as e:
            # R14: push error marker and free slot
            error_marker = {
                "role": "system",
                "content": (
                    f"[BACKGROUND_BRANCH_ERROR] branch_id={branch_id} "
                    f"error={type(e).__name__}: {str(e)[:500]}"
                ),
                "branch": branch_id,
                "error": True,
                "error_type": type(e).__name__,
                "error_message": str(e)[:500],
            }
            _merge_queue.put(error_marker)
            record_branch_error(e)
            _release_branch(branch_id, result_container, "error")
            print(f"BACKGROUND_ERROR: branch_id={branch_id} error={type(e).__name__}: {e}")
            return

    # Check if we exited due to a runaway backstop (or the branch step ceiling)
    if stop_reason is None and branch_steps >= BRANCH_STEP_BUDGET:
        stop_reason = f"it reached the branch step ceiling ({BRANCH_STEP_BUDGET} steps)"
    if stop_reason is not None and branch_sent:
        print(f"BRANCH_BACKSTOP_AFTER_ANSWER: branch_id={branch_id} steps={guard.steps}")
    elif stop_reason is not None:
        StepGuard.paused_by_backstop = True
        # R20: Queue Tier-1 checkpoint data to main thread (never write files directly)
        checkpoint_data = extract_tier1_checkpoint(branch_messages, guard.steps)
        checkpoint_data["stop_reason"] = stop_reason
        checkpoint_data["branch"] = branch_id
        checkpoint_data["request_ids"] = result_container.get("request_ids")
        checkpoint_data["_checkpoint_payload"] = True
        _merge_queue.put(checkpoint_data)
        print(f"BRANCH_CHECKPOINT_QUEUED: branch_id={branch_id} steps={branch_steps}")

    # Free the branch slot (pop by id: a deadline abandon may already have removed it)
    _release_branch(branch_id, result_container, "complete")
    print(f"BRANCH_COMPLETE: branch_id={branch_id} steps={branch_steps}")


def threaded_llm_call(llm_client, model, messages, tools, tool_choice, max_tokens, extra_body):
    """LLM call with deadline ITER_PROMOTE_SECONDS.

    Starts the call in a foreground daemon thread with join timeout T.
    - If the call completes within T: returns (response, None) — identical to direct call.
    - If T expires: promotes to a background branch with a deep copy of messages (R11),
      a separate OpenAI client (R12), and branch id bg-<uuid8>. Returns (None, BranchState).

    The original thread continues running (R9: no token waste). The branch's
    result_container is populated when the thread completes; _active_branches
    holds the branch for merge-queue integration (step 2.2).

    Flag-off: not called; the existing direct client.chat.completions.create path is used.
    """
    result_container = {}
    _promotion_lock(result_container)
    thread = threading.Thread(
        target=_bg_llm_thread_target,
        args=(llm_client, model, messages, tools, tool_choice, max_tokens, extra_body, result_container),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=ITER_PROMOTE_SECONDS)

    if thread.is_alive() and background_branch_count() >= ITER_MAX_BACKGROUND_BRANCHES:
        # The branch set is full: never evict a running branch (that would orphan its
        # thread). Answer this call in the foreground instead.
        print(f"LLM_PROMOTION_SKIPPED: {ITER_MAX_BACKGROUND_BRANCHES} background branch(es) already active; waiting in foreground")
        thread.join(timeout=BG_INFLIGHT_LIMIT)  # slot wait + client timeout + grace
        if thread.is_alive():
            raise Exception("Background LLM call failed: APITimeoutError: foreground call exceeded BG_INFLIGHT_LIMIT")

    promoted = False
    if thread.is_alive():
        # Deadline expired — promote to background branch
        branch_id = f"bg-{uuid.uuid4().hex[:8]}"
        request_ids = dict(_foreground_request_ids) if ITER_REQUEST_BINDING else {}
        with _promotion_lock(result_container):
            if "ok" not in result_container:  # else the call finished meanwhile: keep it in the foreground
                promoted = True
                result_container["request_ids"] = request_ids if ITER_REQUEST_BINDING else None
                fg_guard = globals().get("_foreground_step_guard")  # PROGRESS_NOT_PAUSE: read by the thread
                if fg_guard is not None and hasattr(fg_guard, "fork"):
                    result_container["step_guard"] = fg_guard.fork()
                bg_messages = copy.deepcopy(messages)
                bg_client = openai.OpenAI(
                    api_key=API_KEY, base_url=BASE_URL,
                    timeout=LLM_TIMEOUT, max_retries=0,
                    default_headers={"X-APC-Tenant": "iter-bg", "x-session-id": SESSION_ID, "x-branch-id": branch_id},
                )
                # M3 Step 3.5: store branch client + messages for mini-loop access (R20)
                result_container["branch_client"] = bg_client
                result_container["branch_messages"] = bg_messages
                result_container["branch_id"] = branch_id  # R14: set last; the thread reads it under the lock
        if not promoted:
            print(f"LLM_PROMOTION_RACE_AVOIDED: call finished at the deadline; answered in the foreground")
    if promoted:
        branch = BranchState(
            branch_id=branch_id,
            branch_client=bg_client,
            branch_messages=bg_messages,
            thread=thread,
            result_container=result_container,
            request_ids=request_ids,
        )
        _register_branch_activity(branch_id)
        with _branch_lock:
            _active_branches[branch_id] = branch
            active_count = len(_active_branches)
        if result_container.get("finished"):  # branch finished before it was registered
            _release_branch(branch_id, result_container, "complete")
        if ITER_REQUEST_BINDING:
            _foreground_request_ids.clear()  # the branch now owns these requests
        print(f"LLM_PROMOTED: branch_id={branch_id} deadline={ITER_PROMOTE_SECONDS}s "
              f"active={active_count}/{ITER_MAX_BACKGROUND_BRANCHES} "
              f"requests={sorted(rid[:12] for rid in request_ids.values())}")
        return None, branch

    # Call completed within deadline
    if result_container.get("ok"):
        return result_container["response"], None
    original = result_container.get("exception")
    if original is not None:
        raise original  # K9: keep type, status_code and response (Retry-After) for the classifiers
    raise Exception(f"Background LLM call failed: {result_container.get('error', 'unknown')}")

# --- M2 Step 2.2: Merge queue + double drain (R15) + single-writer + tagged entries (R16) ---
_merge_queue = queue.Queue()  # background→main merge queue (R15)

def _build_tool_call_map(messages):
    """Build a map from tool_call_id → (tool_name, arguments) from assistant messages.

    Used by drain_merge_queue for R16 supersede detection.
    """
    lookup = {}
    for entry in messages:
        if entry.get("role") == "assistant" and entry.get("tool_calls"):
            for tc in entry["tool_calls"]:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    lookup[tc.get("id")] = (fn.get("name", ""), fn.get("arguments", ""))
    return lookup

def drain_merge_queue():
    """Drain all pending entries from the merge queue into experience (R15).

    Pops all entries non-blocking, ensures each has a "branch" tag (R16),
    annotates duplicate tool calls with "superseded_by" (R16), extends
    experience, and saves. Single-writer: only called from the main loop
    thread (R6 single-writer discipline).

    Returns the number of entries merged. When the queue is empty (flag off
    or no active branch), returns 0 and is a pure no-op.
    """
    # Collect all pending entries, separating checkpoint payloads (R20) from regular entries
    merged_entries = []
    checkpoint_payloads = []
    done_payloads = []
    while True:
        try:
            entry = _merge_queue.get_nowait()
        except queue.Empty:
            break
        if not isinstance(entry, dict):
            continue
        # R20: detect checkpoint payloads queued by background branch — handle separately
        if entry.get("_checkpoint_payload"):
            checkpoint_payloads.append(entry)
            continue
        if entry.get("_branch_done"):
            done_payloads.append(entry)
            continue
        # Ensure branch tag is present (R16)
        if "branch" not in entry:
            entry["branch"] = "unknown"
        merged_entries.append(entry)

    # R20: Main thread handles checkpoint file write + send (single-writer discipline)
    for cp in checkpoint_payloads:
        cp_data = {k: v for k, v in cp.items() if k not in ("branch", "_checkpoint_payload", "request_ids")}
        cp_path = write_checkpoint_file(cp_data)
        if cp_path:
            print(f"BRANCH_CHECKPOINT_FILE_WRITTEN: {cp_path}")
        else:
            print("BRANCH_CHECKPOINT_FILE_WRITE_FAILED — will still attempt send")
        _call_bound(cp.get("request_ids"), send_checkpoint_message, cp_data, cp_path)
        # Add a system marker to experience so the LLM sees the branch checkpoint
        experience.append({
            "role": "system",
            "content": f"[BACKGROUND_BRANCH_CHECKPOINT] branch={cp.get('branch', '?')} steps={cp_data.get('step_count', '?')} file={cp_path}",
            "branch": cp.get("branch", "unknown"),
        })

    # Multi-branch: close requests that finished branches left without a final answer
    for done in done_payloads:
        _close_branch_requests(done)

    if not merged_entries:
        save_experience(experience) if checkpoint_payloads else None
        return len(checkpoint_payloads)

    # R16: supersede annotation for duplicate tool calls
    # Build tool_call_id → (tool_name, arguments) from merged assistant entries
    merged_call_lookup = _build_tool_call_map(merged_entries)

    # Build (tool_name, arguments) → first tool-entry index from existing experience
    existing_call_lookup = _build_tool_call_map(experience)
    existing_tool_index = {}  # (tool_name, arguments) → index in experience
    for i, entry in enumerate(experience):
        if entry.get("role") != "tool":
            continue
        tc_id = entry.get("tool_call_id")
        if tc_id and tc_id in existing_call_lookup:
            key = existing_call_lookup[tc_id]
            if key not in existing_tool_index:
                existing_tool_index[key] = i  # first occurrence only

    # Annotate duplicate tool entries with superseded_by (R16)
    for entry in merged_entries:
        if entry.get("role") != "tool":
            continue
        tc_id = entry.get("tool_call_id")
        if tc_id and tc_id in merged_call_lookup:
            key = merged_call_lookup[tc_id]
            if key in existing_tool_index:
                entry["superseded_by"] = str(existing_tool_index[key])

    # Append all entries to experience (R6: single-writer — only main thread)
    for entry in merged_entries:
        experience.append(entry)
    merged = len(merged_entries)
    save_experience(experience)
    print(f"MERGE_DRAINED: {merged} entries from background branch(es)")
    return merged

# --- M3 Step 3.1: BACKGROUND_DEADLINE — abandon + marker + slot frees (R13) ---
def check_background_deadline():
    """Abandon every background branch idle longer than BACKGROUND_DEADLINE (R13).

    For each branch whose last progress (LLM response or tool result; creation
    time if none yet) is older than BACKGROUND_DEADLINE seconds:
    - its results are discarded and its thread is told to stop;
    - an abandon marker is queued to _merge_queue with branch id and timing;
    - its slot is freed, allowing a new promotion, and its request is handed
      back to the main thread for closing.

    Returns True if any branch was abandoned, False otherwise.
    Flag-off: never called; _active_branches is always empty when flag is off.
    """
    now = time.time()
    abandoned = []
    with _branch_lock:
        for branch_id, branch in list(_active_branches.items()):
            elapsed = now - branch.created_at
            # R13 (revised): idle deadline, measured from the branch's last progress,
            # not from creation. A branch that keeps making progress is bounded by
            # BRANCH_STEP_BUDGET, not by wall-clock.
            last_progress = branch.result_container.get("last_progress_at", branch.created_at)
            idle = now - last_progress
            if idle <= BACKGROUND_DEADLINE:
                continue
            inflight_since = branch.result_container.get("llm_inflight_since")
            if inflight_since is not None and now - inflight_since <= BG_INFLIGHT_LIMIT:
                continue  # a call is in flight within its own timeout: it will return, fail or retry
            has_result = bool(branch.result_container.get("ok", False))
            branch.result_container["abandoned"] = True  # tells the branch thread to stop
            branch.result_container["finished"] = True
            del _active_branches[branch_id]  # free the slot (R13)
            abandoned.append((branch, elapsed, idle, has_result))
    for branch, elapsed, idle, has_result in abandoned:
        branch_id = branch.branch_id
        _merge_queue.put({
            "role": "system",
            "content": (
                f"[BACKGROUND_BRANCH_ABANDONED] branch_id={branch_id} "
                f"elapsed={elapsed:.1f}s idle={idle:.1f}s deadline={BACKGROUND_DEADLINE}s. "
                f"LLM call completed: {has_result}. Results discarded."
            ),
            "branch": branch_id,
            "abandoned": True,
        })
        if branch.request_ids:
            _merge_queue.put({"_branch_done": True, "branch": branch_id, "reason": "abandoned",
                              "request_ids": dict(branch.request_ids)})
        print(f"BACKGROUND_ABANDONED: branch_id={branch_id} elapsed={elapsed:.1f}s idle={idle:.1f}s deadline={BACKGROUND_DEADLINE}s llm_completed={has_result}")
    return bool(abandoned)

# --- M3 Step 3.3: Shutdown protocol (R17: SIGTERM/SIGINT stop event, 5s grace, drain, save, exit) ---
SHUTDOWN_GRACE = int(os.getenv("ITER_SHUTDOWN_GRACE", "5"))
_shutdown_event = threading.Event()

def _iter_signal_handler(signum, frame):
    """Signal handler for SIGTERM/SIGINT: set the shutdown event (R17).

    Best-effort: only sets the event; the main loop performs the actual
    graceful shutdown in graceful_shutdown().
    """
    try:
        sig_name = signal.Signals(signum).name
    except (AttributeError, ValueError):
        sig_name = str(signum)
    print(f"SHUTDOWN_SIGNAL: {sig_name} received, setting shutdown event")
    _shutdown_event.set()

def _install_signal_handlers():
    """Install SIGTERM/SIGINT handlers for graceful shutdown (R17).

    Best-effort: if signal installation fails (e.g. not in main thread),
    logs the error and continues — the loop still checks _shutdown_event
    which can be set by other means.
    """
    try:
        signal.signal(signal.SIGTERM, _iter_signal_handler)
        signal.signal(signal.SIGINT, _iter_signal_handler)
        print("SIGNAL_HANDLERS_INSTALLED: SIGTERM, SIGINT")
    except (ValueError, OSError) as e:
        print(f"SIGNAL_HANDLER_INSTALL_FAILED: {type(e).__name__}: {e}")

def graceful_shutdown():
    """Perform graceful shutdown: wait grace, drain, save, exit (R17).

    Called from the main loop when _shutdown_event is set.
    - Wait up to SHUTDOWN_GRACE seconds for any active branch to push results.
    - Drain the merge queue (R15).
    - Save experience (R6: single-writer — only main thread calls save).
    - Exit cleanly. Branch threads are daemon threads so they cannot block exit.
    """
    print(f"SHUTDOWN: grace={SHUTDOWN_GRACE}s")
    deadline = time.time() + SHUTDOWN_GRACE
    while time.time() < deadline:
        with _branch_lock:
            branches = list(_active_branches.values())
        # Stop waiting once every branch thread has a result (ok key present = success or error)
        if all(branch.result_container.get("ok") is not None for branch in branches):
            break
        time.sleep(0.1)
    drained = drain_merge_queue()
    save_experience(experience)
    print(f"SHUTDOWN_COMPLETE: drained={drained} experience_len={len(experience)}")
    sys.exit(0)

# --------------------------------------------------------------------
# 3. Dynamic components:
# --------------------------------------------------------------------
def load_tools():
    inops = {}
    errors = []
    paths = [path for path in sorted(Path("tools").glob("*.py")) if not path.name.startswith("_")]
    for path in paths[:MAX_TOOLS]:
        try:
            result = invoke_dynamic(path, "__tool_metadata__")
            if not result["ok"]:
                raise RuntimeError(result["error"])
            metadata = result["result"]
            inops[path.stem] = (path, metadata["description"], metadata["parameters"])
        except Exception as error:
            errors.append(f"[TOOL ERROR in {path}: {type(error).__name__}: {error}. Repair {path} if needed.]")
    return inops, len(paths) - MAX_TOOLS, "\n".join(errors)

def native_tools(inops):
    tools = []
    for name, (path, description, parameters) in inops.items():
        tools.append({"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": { parameter: { "type": "string" } for parameter in parameters }, "required": [parameter for parameter in parameters], "additionalProperties": False}}})
    return tools

def load_transformation_descriptions():
    entries = []
    for path in sorted(Path("transformations").glob("*.py")):
        if path.name.startswith("_"):
            continue
        result = invoke_dynamic(path, "__description__")
        if result["ok"]:
            entries.append(f"{path.stem}: {result['result']}")
        else:
            entries.append(f"{path.stem}: [DESCRIPTION MISSING]")
    return "\n".join(entries)

def apply_transformation(messages, tools):
    errors = []
    paths = sorted(path for path in Path("transformations").glob("*.py") if not path.name.startswith("_"))
    for path in paths:
        try:
            result = invoke_dynamic(path, "transform", messages, tools)
            if not result["ok"]:
                raise RuntimeError(result["error"])
            messages, tools = result["result"]
        except Exception as error:
            errors.append(f"[RUNTIME ERROR in {path}: {type(error).__name__}: {error}. Repair {path} if needed.]")
    return messages, tools, "\n".join(errors)

# --------------------------------------------------------------------
# 4. Main loop
# --------------------------------------------------------------------
try:
    with open("experience.json", "r", encoding="utf-8") as file:
        experience = json.load(file)
except FileNotFoundError:
    experience = []
SESSION_ID = str(uuid.uuid4())
client = openai.OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=LLM_TIMEOUT, max_retries=0, default_headers={"X-APC-Tenant": "iter", "x-session-id": SESSION_ID})
time.sleep(INIT_WAIT)
Path("memory").mkdir(exist_ok=True)
Path("transformations").mkdir(exist_ok=True)
post_task_mode, autonomous_steps, new_burst = False, 0, True
_last_autonomous_ts = 0.0
retryable_failures, capped_waits, provider_idle = 0, 0, False
send_since_checkpoint = False
step_guard = _foreground_step_guard = StepGuard(has_request=False)  # PROGRESS_NOT_PAUSE
_tool_context.request_ids = _foreground_request_ids if ITER_REQUEST_BINDING else None
pending_event_append = resume_claimed()
cleanup_interval = MAX_EXPERIENCE_SIZE - RETAIN_EXPERIENCE_SIZE
cleanup_bucket = len(experience) // cleanup_interval
if ITER_CONCURRENCY_ENABLED:
    _install_signal_handlers()
while True:
    # M3 Step 3.3: shutdown check (R17: SIGTERM/SIGINT → stop event → grace → drain → save → exit)
    if ITER_CONCURRENCY_ENABLED and _shutdown_event.is_set():
        graceful_shutdown()
    current_bucket = len(experience) // cleanup_interval
    if current_bucket > cleanup_bucket:
        for i, old_message in enumerate(experience):
            if i < len(experience) - RETURN_VALUE_PRESERVE_MESSAGES:
                if old_message.get("role") == "tool" and len(old_message.get("content", "")) > RETURN_VALUE_PRESERVE:
                    old_message["content"] = old_message.get("content", "")[:RETURN_VALUE_PRESERVE] + " [TRUNCATED]"
                for key in ("reasoning", "reasoning_details", "reasoning_content"):
                    old_message.pop(key, None)
        cleanup_bucket = current_bucket
    if len(experience) >= MAX_EXPERIENCE_SIZE:
        experience = experience[-RETAIN_EXPERIENCE_SIZE:]
        cleanup_bucket = len(experience) // cleanup_interval
    while experience and experience[0].get("role") == "tool":
        experience = experience[1:]
    # M3 Step 3.1: check background deadline before drain (R13)
    if ITER_CONCURRENCY_ENABLED:
        check_background_deadline()
    # M2 Step 2.2: double drain — (a) top of loop before receive() (R15)
    if ITER_CONCURRENCY_ENABLED:
        drain_merge_queue()
    history_checkpoint = len(experience) #before user input
    try:
        time.sleep(DEFAULT_DELAY)
        branch_error = pop_branch_error() if ITER_CONCURRENCY_ENABLED else None
        if branch_error is not None:
            raise branch_error  # same terminal/backoff handling as a foreground failure
        print("BEFORE RECEIVE")
        event_append = pending_event_append or receive()
        print("AFTER RECEIVE")
        if event_append:
            provider_idle = False
        elif provider_idle or (ITER_CONCURRENCY_ENABLED and background_branch_active()
                               and not (ITER_REQUEST_BINDING and _foreground_request_ids)):
            # A background branch owns the task, or the provider gave up: only poll for input.
            # Starting another "continue" turn here would run a second worker on stale context.
            time.sleep(BRANCH_IDLE_POLL_SECONDS)
            continue
        if event_append:
            autonomous_steps, new_burst, post_task_mode = 0, False, False
            send_since_checkpoint = False
            step_guard = _foreground_step_guard = StepGuard(has_request=True)
            print("IN FROM CHANNEL " + event_append)
            resume_note = ""
            if StepGuard.paused_by_backstop:
                StepGuard.paused_by_backstop = False
                resume_note = ("[NOTE: an earlier request was stopped by a runaway backstop (step ceiling, "
                               "repeated identical steps, or no new results) and the user was told why. If the "
                               "message below asks you to continue, resume that task from your recent actions "
                               "and notes instead of restarting it, and do not repeat work already done.]\n")
            experience += [{"role": "user", "content": "Step " + get_current_time() + ": " + resume_note + event_append}]
            save_experience(experience)
            if ITER_CONCURRENCY_ENABLED:
                _note_inbound(event_append)
            pending_event_append = ""
            base_temporary_message = []
        elif new_burst:
            post_task_mode, new_burst = True, False
            _last_autonomous_ts = time.time()  # the throttle window starts at this post-task turn
            base_temporary_message = [{"role": "user", "content": "Step " + get_current_time() + ": [TASK COMPLETED. DO NOT RE-SEND THE COMPLETED RESPONSE BUT SEND IN CASE YOU FORGOT. NOW QUERY FOR AND PICK A TASK BASED ON YOUR GOALS, PREFERABLY MEMORY CONSOLIDATION: FINDING EPISODES WHICH SUPPORT / CONTRADICT LTM ITEMS, LINKING EPISODES, PROMOTING USEFUL MEMORIES]"}]
        elif post_task_mode:
            if time.time() - _last_autonomous_ts < AUTONOMOUS_CHECKIN_INTERVAL:
                time.sleep(BRANCH_IDLE_POLL_SECONDS)
                continue  # idle: keep polling receive() cheaply without firing an LLM turn
            _last_autonomous_ts = time.time()
            base_temporary_message = [{"role": "user", "content": "Step " + get_current_time() + ": [NO NEW USER INPUT. CONTINUE AUTONOMOUS WORK. DO NOT REPEAT THE PREVIOUS RESPONSE. ONLY USE send FOR GENUINELY NEW INFORMATION OR WHEN USER INPUT IS NEEDED.]"}]
        else:
            base_temporary_message = [{"role": "user", "content": "Step " + get_current_time() + ": [NO ADDITIONAL USER INPUT. CONTINUE THE CURRENT USER TASK.]"}]
        history_checkpoint = len(experience) #as we want not to loose user input even when exception
        retry_message = None
        _promoted = False
        last_text_only_content = None
        text_only_idle, text_only_retries = False, 0
        while True:
            temporary_message = list(base_temporary_message)
            if retry_message:
                temporary_message += retry_message
            INOPS, omitted_tools, tool_load_error = load_tools()
            if tool_load_error:
                temporary_message += [{"role": "user", "content": tool_load_error}]
            if omitted_tools > 0:
                temporary_message += [{"role": "user", "content": f"[TOOL LIMIT REACHED: {omitted_tools} tools are currently omitted. Consolidate or remove tools if they are needed.]"}]
            TOOLS = native_tools(INOPS)
            TRANSFORMATIONS = load_transformation_descriptions()
            memory_paths = [path for path in sorted(Path("memory").rglob("*")) if path.is_file() and not any(part.startswith("_") for part in path.relative_to("memory").parts)]
            memory_contents = [(path, path.read_text(encoding="utf-8", errors="replace").strip()) for path in memory_paths]
            memory_len = sum(len(content) for _, content in memory_contents)
            if memory_len <= MAX_MEMORY_CHARS:
                MARGIN = MAX_MEMORY_CHARS - memory_len
                MEMORY = f"[{MARGIN} CHARACTERS BELOW MAXIMUM]\n./memory/:\n"
                MEMORY += "\n\n".join(f"{path}:\n{content}" for path, content in memory_contents)
            else:
                DIFF = memory_len - MAX_MEMORY_CHARS
                MEMORY = "./memory/:\n" + "\n".join(str(path) for path in memory_paths)
                temporary_message += [{"role": "user", "content": f"[MEMORY FOLDER TOTAL CHARACTER CAPACITY BY FILES NOT BEGINNING WITH _ EXCEEDED BY {DIFF} CHARS. FIX THIS FIRST.]"}]
            # M2 Step 2.2: double drain — (b) immediately before building messages (R15)
            if ITER_CONCURRENCY_ENABLED:
                drain_merge_queue()
                foreground_view = sibling_view(None)
                if foreground_view:
                    temporary_message += [{"role": "user", "content": foreground_view}]
            request_messages = [{"role": "system", "content": "prompt.txt:\n" + open("prompt.txt", encoding="utf-8", errors="replace").read().strip() + "\n\n./transformations/:\n" + TRANSFORMATIONS + "\n\n" + MEMORY}] + experience + temporary_message
            request_messages, request_tools, transformation_error = apply_transformation(request_messages, TOOLS)
            if transformation_error:
                request_messages += [{"role": "user", "content": transformation_error}]
            print("BEFORE LLM")
            if ITER_CONCURRENCY_ENABLED:
                response, _branch = threaded_llm_call(client, MODEL, request_messages, request_tools, "required", MAX_TOKENS, {"enable_thinking": True})
                if response is None:
                    _promoted = True
                    break
            else:
                response = client.chat.completions.create(model=MODEL, messages=request_messages, tools=request_tools, tool_choice="required", max_tokens=MAX_TOKENS, extra_body={ "enable_thinking": True})
            print("AFTER LLM", response)
            message = response.choices[0].message
            last_text_only_content = message.content if not message.tool_calls else None
            if message.content:
                message.content += "\n[NOT DELIVERED TO ANY CHANNEL. IF THIS WAS INTENDED AS COMMUNICATION, USE send.]"
            if message.tool_calls:
                message.tool_calls = message.tool_calls[:MAX_TOOL_CALLS]
                break
            if response.choices[0].finish_reason != "length":
                # Text-only reply: in autonomous mode it means "nothing to do"; otherwise re-prompt at most TEXT_ONLY_RETRY_CAP times.
                if post_task_mode or text_only_retries >= TEXT_ONLY_RETRY_CAP:
                    print(f"TEXT_ONLY_IDLE: post_task_mode={post_task_mode} retries={text_only_retries}")
                    text_only_idle = True
                    break
                text_only_retries += 1
            try:
                if response.choices[0].finish_reason == "length":
                    retry_message = [{"role": "user", "content": "[OUTPUT TOKEN LIMIT REACHED. CALL THE REQUIRED TOOL CONCISELY.]"}]
                else:
                    retry_message = [{"role": "user", "content": f"[YOUR PREVIOUS RESPONSE CONTAINED NO TOOL CALL AND WAS NOT DELIVERED. CALL AT LEAST ONE TOOL NOW. IF YOU INTENDED THIS CONTENT AS COMMUNICATION, USE send: {message.content!r}]"}]
            except:
                retry_message = [{"role": "user", "content": f"[YOUR PREVIOUS RESPONSE CONTAINED NO TOOL CALL AND WAS NOT DELIVERED. CALL AT LEAST ONE TOOL NOW. IF YOU INTENDED THIS CONTENT AS COMMUNICATION, USE send: {message.content!r}]"}]
        if _promoted:
            if ITER_REQUEST_BINDING:
                new_burst = True  # the branch owns this task now; the foreground is free
            step_guard = _foreground_step_guard = StepGuard(has_request=False)  # the branch forked the count
            continue
        if text_only_idle:
            retryable_failures, capped_waits = 0, 0
            if not post_task_mode and not send_since_checkpoint:
                # A user request is still unanswered and the model kept replying with text only:
                # deliver that text instead of silently dropping it.
                if send_text_only_fallback(last_text_only_content):
                    send_since_checkpoint = True
            if not post_task_mode:
                new_burst = True
            autonomous_steps = 0
            step_guard = _foreground_step_guard = StepGuard(has_request=False)
            pending_event_append = slow_wait_for_input()
            continue
        retryable_failures, capped_waits = 0, 0
        print(f"RESPONSE {response}\nFINISH_REASON {response.choices[0].finish_reason}\nUSAGE {response.usage}")
        experience += [{key: value for key, value in message.model_dump(exclude_none=True).items() if KEEP_REASONING_IN_EPISODE or key not in ("reasoning", "reasoning_details", "reasoning_content")}]
        tool_outputs = []
        step_results = []
        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            try:
                tool_arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as error:
                tool_arguments = tool_call.function.arguments
                ret = f"Invalid tool arguments from model: {error}"
            else:
                try: #unless tool unknown/args formatting issue, we use the tool's INOPS function return value:
                    if tool_name not in INOPS:
                        ret = f"Unknown tool: {tool_name!r}"
                    elif not isinstance(tool_arguments, dict):
                        ret = "Tool arguments must be a JSON object"
                    else:
                        result = invoke_dynamic(INOPS[tool_name][0], "run", **tool_arguments)
                        ret = result["result"] if result["ok"] else f"Tool execution failed: {result['error']}"
                        if tool_name == "send" and result["ok"] and str(result["result"]).startswith("SUCCESS"):
                            _note_send("foreground", tool_arguments.get("content"))
                except Exception as error:
                    ret = f"Tool execution failed: {type(error).__name__}: {error}"
            ret = str(ret)
            step_results.append((tool_name, tool_call.function.arguments, ret))
            if len(ret) > MAX_TOOL_OUTPUT_CHARS:
                ret = ret[:MAX_TOOL_OUTPUT_CHARS] + " [TRUNCATED]"
            experience += [{"role": "tool", "tool_call_id": tool_call.id, "content": "Step " + get_current_time() + ": " + ret}]
            tool_outputs += ["tool call: " + tool_name + " " + str(tool_arguments) + "\n" "tool return: " + ret]
        history_checkpoint = len(experience) #tool calls succeeded, even on later exception we won't unroll them
        save_experience(experience)
        print("Output> " + "\n".join(tool_outputs))
        autonomous_steps = 0 if event_append else autonomous_steps + 1
        called_send = any(call.function.name == "send" for call in message.tool_calls)
        called_nop = any(call.function.name == "nop" for call in message.tool_calls)
        # PROGRESS_NOT_PAUSE: interim notes every PROGRESS_EVERY steps instead of a stop;
        # only runaway backstops (ceiling / repeated / no new results) stop the request.
        guard_action, guard_text = step_guard.record_step(message.tool_calls, step_results)
        if step_guard.answered:
            send_since_checkpoint = True  # a final send answered the request (progress notes do not)
        if called_nop:
            if not post_task_mode:
                new_burst = True  # a nop during autonomous work must not re-arm the post-task prompt (bypassed the throttle)
            autonomous_steps = 0
            step_guard = _foreground_step_guard = StepGuard(has_request=False)
            close_unfinished_requests()
            pending_event_append = slow_wait_for_input()
        elif guard_action == "stop":
            print(f"STEP BACKSTOP: steps={step_guard.steps} reason={guard_text}")
            if step_guard.has_request and not step_guard.answered:
                StepGuard.paused_by_backstop = True
                # M1 Step 1.1: Tier-1 mechanical checkpoint (file is flag-guarded); the stop note states why
                _checkpoint_data = extract_tier1_checkpoint(experience, step_guard.steps)
                _checkpoint_data["stop_reason"] = guard_text
                _checkpoint_path = write_checkpoint_file(_checkpoint_data) if ITER_CHECKPOINT_ENABLED else None
                if _checkpoint_path:
                    print(f"CHECKPOINT_FILE_WRITTEN: {_checkpoint_path}")
                # M1 Step 1.3: human-readable send via normal tool path
                send_checkpoint_message(_checkpoint_data, _checkpoint_path)
            autonomous_steps = 0
            # Really stop: the old stop fell through to "CONTINUE THE CURRENT USER TASK" on the next
            # loop.  Go to throttled post-task mode instead (provider-silent until new input or the
            # AUTONOMOUS_CHECKIN_INTERVAL check-in).
            post_task_mode, new_burst = True, False
            _last_autonomous_ts = time.time()
            step_guard = _foreground_step_guard = StepGuard(has_request=False)
            close_unfinished_requests()
            pending_event_append = slow_wait_for_input()
        elif guard_action in ("nudge", "warn"):
            print(f"PROGRESS {guard_action.upper()} step={step_guard.steps}")
            experience += [{"role": "user", "content": "Step " + get_current_time() + ": " + guard_text}]
            save_experience(experience)
        elif guard_action == "fallback":
            delivered = send_progress_note(guard_text)
            print(f"PROGRESS FALLBACK step={step_guard.steps} delivered={delivered}")
    except Exception as error:
        print(f"Output> {type(error).__name__}: {error}")
        experience = experience[:history_checkpoint]
        if not retryable_provider_error(error):
            time.sleep(ERROR_RECOVERY_TIME)
            continue
        retryable_failures += 1
        delay = retry_backoff_delay(retryable_failures, retry_after_seconds(error))
        capped_waits = capped_waits + 1 if delay >= RETRY_BACKOFF_CAP else 0
        if provider_quota_exhausted(error) or capped_waits >= RETRY_CAP_HITS_BEFORE_IDLE:
            print(f"PROVIDER_EXHAUSTED_IDLE: failures={retryable_failures} quota={provider_quota_exhausted(error)}")
            send_provider_exhausted_notice(error)
            autonomous_steps, new_burst, post_task_mode = 0, False, False
            retryable_failures, capped_waits = 0, 0
            provider_idle = True
            pending_event_append = slow_wait_for_input()
            continue
        print(f"PROVIDER_BACKOFF: failures={retryable_failures} delay={delay:.1f}s")
        pending_event_append = backoff_wait(delay)
