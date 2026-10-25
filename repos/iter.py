import contextlib
import copy
import datetime
import hashlib
import inspect
import json
import os
import re
import sys
import openai
import queue
import threading
import time
import importlib.util
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path

# --------------------------------------------------------------------
# 0. Configuration:
# --------------------------------------------------------------------
MAX_MEMORY_CHARS = 8000 #When memory turns into an index (raised from 3000 2026-09-17: protomega's legitimate self-curated memory hit the cap, went filename-only, and it silently lost a git-tool note as a result)
LLM_TIMEOUT = int(os.getenv("ITER_PROVIDER_TIMEOUT_SECONDS", "120"))
KEEP_REASONING_IN_EPISODE = True
PRINT_CALLS = False
MAX_TOOL_CALLS = 10
MAX_TOOL_OUTPUT_CHARS = 5000
MAX_EXPERIENCE_SIZE = 100   #20 percent
RETAIN_EXPERIENCE_SIZE = 80 #jumps
ALARM_MAX_LATE_SECONDS = 6 * 3600  # a reminder later than this is expired, not sent
ALARM_CHECK_INTERVAL = 5
DUPLICATE_WINDOW_SECONDS = 120  # only a repeat this soon counts as a double delivery
# BEGIN PROGRESS_NOT_PAUSE (2026-10-03, Ben Telegram 7445/7462)
# History: a hard per-request pause at MAX_FAST_STEPS (50 upstream; 8 from
# 2026-09-19 as a spend guard; 30 from 2026-09-21) made long work stop until a
# human typed "continue".  Now every ITER_PROGRESS_EVERY autonomous steps the
# agent posts a NON-closing interim note (channel progress(), not send and not
# publish_terminal_failure) and keeps working.  Only runaway backstops pause
# with the "continue" notice: the hard ceiling, repeated identical steps, and
# steps that produce no new results.  Idle stays provider-silent (see the idle
# branch in the main loop).
def _env_int(name, default, minimum):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)

PROGRESS_EVERY = _env_int("ITER_PROGRESS_EVERY", 30, 3)  # interim update cadence (steps)
PROGRESS_FALLBACK_STEPS = _env_int("ITER_PROGRESS_FALLBACK_STEPS", 2, 1)  # mechanical note if the model has not posted within this many steps of the nudge
HARD_STEP_CEILING = _env_int("ITER_HARD_STEP_CEILING", 300, 3)  # runaway backstop per request
REPEAT_STEPS = _env_int("ITER_REPEAT_STEPS", 8, 2)  # backstop: this many consecutive steps with identical tool calls + arguments
STALL_STEPS = _env_int("ITER_STALL_STEPS", 12, 2)  # backstop: this many consecutive steps whose tool calls/results were all seen before in this request
MAX_FAST_STEPS = HARD_STEP_CEILING  # legacy name used by the idle gate
# END PROGRESS_NOT_PAUSE
# FAILURE_CLIENT (2026-10-23): what follows a failed model call is the route service's answer
# (/episodes/failure, ROUTING.md §9.4), not a local attempt cap. This bounds only errors that are
# not the provider's (the service answers "agent"): that many in a row on one request stop it.
AGENT_ERROR_LIMIT = _env_int("ITER_MAX_AGENT_ERRORS", 2, 1)
FAILURE_FALLBACK_WAIT = _env_int("ITER_FAILURE_FALLBACK_WAIT_SECONDS", 30, 0)  # service unreachable: one retry after this
# BEGIN CONCURRENCY_PORT config (2026-10-12). Spec: bgoertzel-sing/iter branch
# agent/protocosmo2-concurrency-port-reference @ e1745f9, docs/CONCURRENCY_PORT.md;
# record: proto-hive staging/experiments/20261012T2010Z-iter-concurrency-port/RUN.md.
# A model call still running after ITER_PROMOTE_SECONDS becomes a background branch
# that finishes its request in its own thread while the main loop goes back to
# receiving.  ITER_CONCURRENCY_ENABLED=0 (the default) changes nothing.
ITER_CONCURRENCY_ENABLED = os.getenv("ITER_CONCURRENCY_ENABLED", "0") == "1"
ITER_PROMOTE_SECONDS = _env_int("ITER_PROMOTE_SECONDS", 30, 1)  # D2: promotion deadline T
BACKGROUND_DEADLINE = max(2 * ITER_PROMOTE_SECONDS, 300)  # abandon a branch idle this long (R13, B3)
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
# D7/F4: request binding whenever concurrency is on (1 branch = 2 open requests), so
# a chat message can be claimed while a promoted branch still owns its request.
ITER_REQUEST_BINDING = ITER_CONCURRENCY_ENABLED
if ITER_REQUEST_BINDING:
    os.environ.setdefault("ITER_MAX_OPEN_REQUESTS", str(ITER_MAX_BACKGROUND_BRANCHES + 1))
else:
    os.environ["ITER_MAX_OPEN_REQUESTS"] = "1"  # unbound sends need exactly one open request
_foreground_request_ids = {}  # channel stem -> request the foreground answers (kept after it closes, for follow-ups)
_foreground_open = set()      # channel stems whose foreground request may still be open


def _max_concurrent_llm_calls(raw=None):
    """Cap on concurrent model calls (foreground + branches); default branches + 1, clamped 1..default."""
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


BG_INFLIGHT_LIMIT = 2 * LLM_TIMEOUT + 60  # slot wait + client timeout + grace (660 s at LLM_TIMEOUT=300)
BRANCH_STEP_BUDGET = HARD_STEP_CEILING
BRANCH_IDLE_POLL_SECONDS = 1
TEXT_ONLY_RETRY_CAP = 2  # B2: text-only replies re-prompted at most this often (flag on)
try:
    ITER_REQUEST_TIMEOUT = float(os.getenv("ITER_REQUEST_TIMEOUT", "3600"))  # close an orphaned request idle this long; <=0 off
except ValueError:
    ITER_REQUEST_TIMEOUT = 3600.0
EXPIRE_CHECK_INTERVAL = 60
_last_expire_check = 0.0
SHUTDOWN_GRACE = _env_int("ITER_SHUTDOWN_GRACE", 3, 0)  # < the supervisor's 5 s SIGTERM->SIGKILL window
# D1: tool runs keep DYNAMIC_TIMEOUT (5 s, the de-facto shell/tool limit on VM2);
# channel-contract calls and the tools that write the channel get CHANNEL_TIMEOUT,
# because a kill mid-call can strand a claimed request or half-close an answer.
CHANNEL_TIMEOUT = 30
CHANNEL_TIMEOUT_FUNCTIONS = frozenset({
    "receive", "receive_request", "resume", "resume_all", "active_request_ids", "close_active",
    "expire_stale", "notify", "progress", "reminder_source", "deliver_reminder", "send", "__has_attrs__"})
CHANNEL_TIMEOUT_TOOLS = frozenset({"send", "progress", "reminder"})
PROVIDER_FAILURE_TEXT = ("I could not reach the response provider within the bounded retry window. "
                         "The failure was recorded; please retry shortly.")


def provider_stop_text(reason=None):
    """What the person sees when a request stops on a failed model call (FAILURE_CLIENT): the
    route service's reason when there is one (short, fit to show a person), else the old text."""
    if not reason:
        return PROVIDER_FAILURE_TEXT
    return f"I couldn't get an answer from the model for this message: {reason}. The failure was recorded."
# END CONCURRENCY_PORT config
SLOW_STEP_DELAY = 10
ERROR_RECOVERY_TIME = 1 #after how long to retry when exception occurs
RETURN_VALUE_PRESERVE = int(os.getenv("ITER_RETURN_VALUE_PRESERVE", "300"))  # was 0: after ~20 messages every older tool result became "[TRUNCATED]", so a resumed task re-ran its scans
RETURN_VALUE_PRESERVE_MESSAGES = 10
DEFAULT_DELAY = 0 #default delay added irregard of whether in slow mode
MAX_TOKENS = 32000  # was 8192; Opus 5.5 (thinking always on) hit stop=length at 8192 with thinking only (2026-09-26). Was 2524 before 2026-09-16.
INIT_WAIT = 10
MAX_TOOLS = 30
MAX_TOOL_DESCRIPTION_CHARS = 500
DYNAMIC_TIMEOUT = 5

# BEGIN PDF_TURN_IMAGES (2026-09-29, Ben Telegram 7223)
# The Telegram receiver renders low-text PDF pages to PNGs inside this agent's
# private attachments directory and lists each one on a receipt line
#   PDF_PAGE_IMAGE page=N path=/abs/file.png sha256=<hex>
# Attach those images (as OpenAI-style image_url data parts, which the OpenClaw
# chat-completions gateway forwards for the active user turn) to the model
# request only.  experience/history keeps the text; files outside the private
# root, non-PNGs, oversize files, and hash mismatches are ignored.
import base64 as _pdf_b64
import re as _pdf_re
import stat as _pdf_stat
PDF_TURN_IMAGE_MAX = 6
PDF_TURN_IMAGE_MAX_BYTES = 3_000_000
PDF_TURN_IMAGE_TOTAL_BYTES = 8_000_000
_PDF_TURN_IMAGE_RE = _pdf_re.compile(r"^PDF_PAGE_IMAGE page=(\d{1,5}) path=(/\S+\.png) sha256=([0-9a-f]{64})$", _pdf_re.MULTILINE)


def _pdf_turn_image_root():
    override = os.getenv("ITER_ATTACHMENT_IMAGE_ROOT", "")
    if override:
        return Path(override)
    channel_root = os.getenv("ITER_OUTER_CHANNEL_ROOT", "")
    if channel_root:
        return Path(channel_root).parent / "attachments"
    return None


def attach_turn_pdf_images(request_messages):
    """Return a request copy whose final user message carries the PDF page images."""
    try:
        if not request_messages:
            return request_messages
        last = request_messages[-1]
        if not isinstance(last, dict) or last.get("role") != "user" or not isinstance(last.get("content"), str):
            return request_messages
        trailing = []
        for message in reversed(request_messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                break
            if isinstance(message.get("content"), str):
                trailing.append(message["content"])
        matches = []
        for text in reversed(trailing):
            matches.extend(_PDF_TURN_IMAGE_RE.findall(text))
        if not matches:
            return request_messages
        root = _pdf_turn_image_root()
        if root is None or not root.is_dir() or root.is_symlink():
            return request_messages
        root_real = os.path.realpath(root)
        parts, seen, total = [], set(), 0
        for _page, path, digest in matches:
            if len(parts) >= PDF_TURN_IMAGE_MAX:
                break
            real = os.path.realpath(path)
            if real != path or not real.startswith(root_real + os.sep) or real in seen:
                continue
            try:
                info = os.lstat(real)
            except OSError:
                continue
            if (not _pdf_stat.S_ISREG(info.st_mode) or info.st_size > PDF_TURN_IMAGE_MAX_BYTES
                    or total + info.st_size > PDF_TURN_IMAGE_TOTAL_BYTES):
                continue
            with open(real, "rb") as handle:
                data = handle.read(PDF_TURN_IMAGE_MAX_BYTES + 1)
            if (len(data) > PDF_TURN_IMAGE_MAX_BYTES or data[:8] != b"\x89PNG\r\n\x1a\n"
                    or hashlib.sha256(data).hexdigest() != digest):
                continue
            seen.add(real)
            total += len(data)
            parts.append({"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + _pdf_b64.b64encode(data).decode("ascii")}})
        if not parts:
            return request_messages
        print(f"PDF_TURN_IMAGES_ATTACHED count={len(parts)} bytes={total}")
        return list(request_messages[:-1]) + [dict(last, content=[{"type": "text", "text": last["content"]}] + parts)]
    except Exception as error:
        print(f"PDF_TURN_IMAGES_SKIPPED {type(error).__name__}")
        return request_messages
# END PDF_TURN_IMAGES

MODEL = os.getenv("LLM_MODEL", "mlx-community/gemma-4-26b-a4b-it-4bit")
BASE_URL = os.getenv("BASE_URL", "http://192.168.64.1:2277/v1")
API_KEY = os.getenv("AI_API_KEY", "dummy")

def served_model_tag(response):
    """"[model]" for the model that actually answered, as reported by the gateway
    (after any fallback); "" when unknown, so a message is never mislabelled."""
    served = getattr(response, "served_model", None)
    if not isinstance(served, str) or not served.strip():
        return ""
    name = re.sub(r"-latest$", "", re.sub(r"-\d{8}$", "", served.strip().rsplit("/", 1)[-1]))
    return f"[{name}]" if name else ""

# --------------------------------------------------------------------
# 1. Dynamic execution:
# --------------------------------------------------------------------
# CONCURRENCY_PORT (B6): channel-contract hooks return JSON-native values that the
# caller indexes (receive_request()["request_id"], ...); str() would break them.
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
        elif function == "__has_attrs__":  # CONCURRENCY_PORT F1: probe real callables
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

_tool_context = threading.local()  # CONCURRENCY_PORT: per-thread request binding for subprocesses


def _tool_env():
    """Environment for tool/channel subprocesses: adds ITER_REQUEST_IDS when this thread is bound."""
    ids = getattr(_tool_context, "request_ids", None)
    if ids is None:
        return None
    env = dict(os.environ)
    env["ITER_REQUEST_IDS"] = json.dumps(ids, sort_keys=True)
    return env


# G28: tools that read-modify-write shared state (chroma, metta/atomspace and memory files,
# alarms, the task file) run one at a time across Iter's threads when concurrency is on.
# shell/python can touch anything and are not serialized (accepted risk; RUN.md).
STATEFUL_TOOLS = frozenset({
    "remember", "forget", "memory_update", "metta", "formalize_metta", "link_episode", "unlink_episode",
    "contradict", "support", "confidence_boost", "reminder", "start_new_task", "chroma_query"})
_stateful_tool_lock = threading.Lock()


def _tool_lock(path, function):
    path = Path(path)
    if (ITER_CONCURRENCY_ENABLED and function == "run" and path.parent.name == "tools"
            and path.stem in STATEFUL_TOOLS):
        return _stateful_tool_lock
    return contextlib.nullcontext()


def _dynamic_timeout(path, function):
    """D1 split timeout: CHANNEL_TIMEOUT for channel-contract calls and channel-writing tools."""
    path = Path(path)
    if path.parent.name == "channels" and function in CHANNEL_TIMEOUT_FUNCTIONS:
        return CHANNEL_TIMEOUT
    if path.parent.name == "tools" and function == "run" and path.stem in CHANNEL_TIMEOUT_TOOLS:
        return CHANNEL_TIMEOUT
    return DYNAMIC_TIMEOUT

def _invoke_dynamic_unlocked(path, /, function, *args, **kwargs):
    timeout = _dynamic_timeout(path, function)
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
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if inherit_stdin:
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            return {"ok": False, "error": f"TIMEOUT after {timeout}s"}
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

def invoke_dynamic(path, /, function, *args, **kwargs):
    with _tool_lock(path, function):
        return _invoke_dynamic_unlocked(path, function, *args, **kwargs)

if len(sys.argv) > 1 and sys.argv[1] == "--invoke":
    dynamic_worker()
    sys.exit(0)

# --------------------------------------------------------------------
# 2. Runtime helpers:
# --------------------------------------------------------------------
def get_current_time():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def receive():
    events = []
    paths = [path for path in sorted(Path("channels").glob("*.py")) if not path.name.startswith("_")]
    for path in paths:
        try:
            if ITER_REQUEST_BINDING:  # CONCURRENCY_PORT: claim one foreground request at a time
                bound = _channel_has(path, "receive_request")
                if not bound and not _channel_probed(path):
                    raise RuntimeError("channel hook probe failed; not claiming while request binding is on")
                if bound:
                    if _foreground_busy(path):
                        continue  # one foreground request at a time; new input waits in the inbox
                    result = invoke_dynamic(path, "receive_request")
                    if not result["ok"]:
                        raise RuntimeError(result["error"])
                    claimed = result["result"]
                    if claimed:
                        _foreground_request_ids[path.stem] = claimed["request_id"]
                        _foreground_open.add(path.stem)
                        events.append("[" + path.stem + "] " + str(claimed["prompt"]))
                    continue
            result = invoke_dynamic(path, "receive")
            if not result["ok"]:
                raise RuntimeError(result["error"])
            event = result["result"]
            if event:
                events.append("[" + path.stem + "] " + str(event))
        except Exception as error:
            # A transport diagnostic is not user input and must never authorize
            # a provider call. Keep it local rather than returning a synthetic
            # event that resets the per-event provider budget.
            print(f"CHANNEL ERROR in {path}: {type(error).__name__}: {error}")
    return "\n".join(events)

last_alarm_check = 0.0

def fire_due_alarms():
    """Deliver due reminders written by tools/reminder.py (memory/alarms/).

    No model call: the reminder is queued in the channel's followups/ and the
    receiver sends it as a reply to the message that set it. Each alarm is then
    moved to memory/alarms/done/ and its outcome noted in the history.
    """
    global last_alarm_check, experience
    now = time.time()
    if now - last_alarm_check < ALARM_CHECK_INTERVAL:
        return
    last_alarm_check = now
    alarms = Path("memory") / "alarms"
    if not alarms.is_dir():
        return
    for path in sorted(alarms.iterdir()):
        if not path.is_file() or not path.name[:1].isdigit():
            continue
        try:
            due = int(path.name.split("-")[0].split(".")[0])
        except ValueError:
            continue
        if due > now:
            continue
        message = ""
        try:
            text = path.read_text(encoding="utf-8")
            try:
                alarm = json.loads(text)
            except json.JSONDecodeError:  # legacy "channel\nmessage" format
                channel, _, legacy_message = text.partition("\n")
                alarm = {"channel": channel.strip(), "message": legacy_message, "source": None}
            message = str(alarm.get("message", "")).strip()
            channel_path = Path("channels") / (str(alarm.get("channel", "")) + ".py")
            if now - due > ALARM_MAX_LATE_SECONDS:
                outcome = "expired (more than 6h late, not sent)"
            elif not message or not channel_path.is_file():
                outcome = "invalid (no message or unknown channel)"
            else:
                source = alarm.get("source")
                if not source:
                    looked_up = invoke_dynamic(channel_path, "reminder_source")
                    source = json.loads(looked_up["result"]) if looked_up.get("ok") else None
                if not source:
                    outcome = "undeliverable (no conversation to reply to)"
                else:
                    alarm_id = path.name.split(".")[0].lower()
                    result = invoke_dynamic(channel_path, "deliver_reminder", "\u23f0 Reminder: " + message, source, alarm_id)
                    outcome = "delivered" if result.get("ok") else f"failed: {result.get('error')}"
        except Exception as error:
            outcome = f"failed: {type(error).__name__}: {error}"
        done = alarms / "done"
        done.mkdir(exist_ok=True)
        os.replace(path, done / path.name)
        print(f"ALARM {path.name} {outcome}")
        experience += [{"role": "user", "content": (
            f"Step {get_current_time()}: [REMINDER {outcome}: "
            f"{message[:300]!r} (due {datetime.datetime.utcfromtimestamp(due):%Y-%m-%d %H:%M:%S} UTC). "
            "Iter handled it automatically; do not send it again.]")}]
        save_experience(experience)

def slow_wait_for_input():
    for second in range(SLOW_STEP_DELAY):
        if _shutdown_event.is_set():  # CONCURRENCY_PORT R17 (never set with the flag off)
            return ""
        time.sleep(1)
        fire_due_alarms()
        event_append = receive()
        if event_append:
            return event_append
    return ""

class ProviderStop(RuntimeError):
    """FAILURE_CLIENT: the route service said stop for this request (or non-provider call errors
    repeated AGENT_ERROR_LIMIT times): no more model calls for it; tell the person why."""

    def __init__(self, reason=None, cls=None):
        super().__init__(reason or "the model call kept failing")
        self.reason, self.cls = reason, cls

def publish_terminal_failure(content):
    """Complete the one active routed request with a fixed visible failure."""
    delivered = 0
    for path in sorted(Path("channels").glob("*.py")):
        if path.name.startswith("_"):
            continue
        bound = _bound_request_kwargs(path)  # CONCURRENCY_PORT: {} when unbound
        if bound is None:
            continue
        result = invoke_dynamic(path, "notify", content, **bound)
        if result.get("ok"):
            delivered += 1
    if delivered != 1:
        raise RuntimeError(f"terminal failure delivery count was {delivered}, expected 1")

def _tally_text(tally):
    return ", ".join(f"{name} x{count}" for name, count in sorted(tally.items(), key=lambda kv: -kv[1])) or "none"

def publish_duplicate_notice():
    """Close a request suppressed as a rapid exact repeat, so it cannot strand."""
    try:
        publish_terminal_failure(
            "I skipped this message because it exactly repeats one I received moments ago. "
            "If you meant to send it again, please rephrase it slightly.")
    except RuntimeError as error:
        print(f"DUPLICATE NOTICE SKIPPED: {error}")

def publish_step_limit_notice(steps, tally, reason=None):
    """Answer the active request when the per-event step ceiling is reached.

    The ceiling is a deliberate spend guard (see the idle branch in the main
    loop), but reaching it used to leave the routed request claimed and
    unanswered until the supervisor's --iter-timeout expired 30 minutes later
    and posted a generic failure (2026-09-21, two requests). If the model has
    already answered (send completed the request) there is nothing to close.
    The summary is mechanical (tool-name counts only, no arguments or outputs,
    so nothing sensitive can leak) and costs no model call.
    """
    reason = reason or f"I reached my per-request step ceiling ({HARD_STEP_CEILING} steps)"
    try:
        publish_terminal_failure(
            f"I paused at step {steps} of this request because {reason}, "
            "to avoid runaway usage. "
            f"Actions this run: {_tally_text(tally)}. "
            "My conversation state is saved and any file edits are on disk (not necessarily "
            "committed). Reply 'continue' (or add instructions) and I will resume."
        )
    except RuntimeError as error:
        print(f"STEP LIMIT NOTICE SKIPPED (request already answered?): {error}")

def publish_progress_note(content):
    """Post Iter's own interim note through each channel's progress() path.

    progress() never answers/closes the routed request and never spends one of
    the model's follow-up slots; no model call is made here.  A channel
    without progress() is skipped (never publish_terminal_failure).
    """
    delivered = 0
    for path in sorted(Path("channels").glob("*.py")):
        if path.name.startswith("_"):
            continue
        bound = _bound_request_kwargs(path)  # CONCURRENCY_PORT: {} when unbound
        if bound is None:
            print(f"PROGRESS NOTE SKIPPED on {path.stem}: no request bound to this thread")
            continue
        result = invoke_dynamic(path, "progress", content, **bound)
        if result.get("ok"):
            delivered += 1
        else:
            print(f"PROGRESS NOTE SKIPPED on {path.stem}: {result.get('error')}")
    return delivered

def step_signature(tool_calls):
    """Order-insensitive fingerprint of one step's tool names + arguments."""
    calls = []
    for call in tool_calls:
        try:
            arguments = json.dumps(json.loads(call.function.arguments), sort_keys=True, ensure_ascii=False)
        except Exception:
            arguments = str(call.function.arguments)
        calls.append(call.function.name + "\x00" + arguments)
    return hashlib.sha256("\x01".join(sorted(calls)).encode("utf-8", errors="replace")).hexdigest()

def publish_no_response_notice():
    """Close an open request when the model chose nop without ever calling send.

    nop leaves the claimed request open, so the outer transport waited for the
    full --iter-timeout and then posted "could not complete this message
    safely" (2026-09-21, a Telegram message id redacted for the public repo: the model rightly judged a group reply
    was not for it). The transport has no silent-completion path, so post a
    short neutral line instead of a 30-minute wait and a failure message.

    Only a request that is still open gets the line.  notify() turns a notice
    for an answered request into a follow-up, and a [hive] event resets
    answered_this_event while the binding still points at the last Telegram
    request, so a nop after a hive event re-posted this line under messages
    that were already answered (2026-10-23, three Telegram message ids, redacted for the public repo).
    """
    for path in sorted(Path("channels").glob("*.py")):
        if path.name.startswith("_"):
            continue
        bound = _bound_request_kwargs(path)
        if bound is None:
            continue
        result = invoke_dynamic(path, "active_request_ids")
        if not result.get("ok"):
            continue  # unknown: fall through to notify(), the pre-fix behaviour
        open_ids = result.get("result") or []
        if (bound.get("request_id") not in open_ids) if bound else not open_ids:
            print(f"NO-RESPONSE NOTICE SKIPPED on {path.stem}: request already answered or closed")
            return
    try:
        publish_terminal_failure("I have nothing to add to this message.")
    except RuntimeError as error:
        print(f"NO-RESPONSE NOTICE SKIPPED (request already answered?): {error}")

def save_experience(experience):
    with open("experience.tmp", "w", encoding="utf-8") as file:
        json.dump(experience, file, ensure_ascii=False, indent=2)
    os.replace("experience.tmp", "experience.json")

# --------------------------------------------------------------------
# BEGIN CONCURRENCY_PORT machinery (see the config block at the top)
# --------------------------------------------------------------------
def _bound_request_kwargs(path):
    """Channel-hook kwargs for the calling thread's binding: {} unbound (original contract),
    {"request_id": id} bound, None when bound but owning no request on this channel."""
    if not ITER_REQUEST_BINDING:
        return {}
    ids = getattr(_tool_context, "request_ids", None)
    if ids is None:
        return {}
    rid = ids.get(Path(path).stem)
    return {"request_id": rid} if rid else None


_CHANNEL_PROBE_NAMES = ("receive_request", "resume_all", "active_request_ids", "close_active", "expire_stale")
_channel_probe_cache = {}  # resolved path -> (mtime_ns, size, {name: bool})


def _channel_has(path, name):
    """True if the channel module actually exposes callable *name* once imported (F1/6ea3dd6).

    A text search is not enough (Sep 30 B5 outage). Probed with __has_attrs__ in a
    worker subprocess; successful probes are cached per file mtime/size. A failed probe
    returns False and is not cached (G13), so one slow probe cannot pin a channel to
    the legacy path; callers that must not fall back check _channel_probed()."""
    try:
        stat = Path(path).stat()
    except OSError:
        return False
    key = str(Path(path).resolve())
    cached = _channel_probe_cache.get(key)
    if cached is None or cached[:2] != (stat.st_mtime_ns, stat.st_size) or name not in cached[2]:
        names = tuple(dict.fromkeys(_CHANNEL_PROBE_NAMES + (name,)))
        result = invoke_dynamic(path, "__has_attrs__", *names)
        if not (result.get("ok") and isinstance(result.get("result"), dict)):
            _channel_probe_cache.pop(key, None)
            print(f"CHANNEL_PROBE_FAILED: {path}: {result.get('error')}")
            return False
        found = result["result"]
        cached = (stat.st_mtime_ns, stat.st_size, {n: bool(found.get(n)) for n in names})
        _channel_probe_cache[key] = cached
    return cached[2][name]


def _channel_probed(path):
    """True when the channel's current file was probed successfully."""
    try:
        stat = Path(path).stat()
    except OSError:
        return False
    cached = _channel_probe_cache.get(str(Path(path).resolve()))
    return cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size)


def _foreground_busy(path):
    """True while the foreground's own request on this channel is still open."""
    if path.stem not in _foreground_open:
        return False
    rid = _foreground_request_ids.get(path.stem)
    if rid is None:
        _foreground_open.discard(path.stem)
        return False
    result = invoke_dynamic(path, "active_request_ids")
    if not result.get("ok"):
        return True  # unknown: never claim a second foreground request
    if rid in (result.get("result") or []):
        return True
    _foreground_open.discard(path.stem)  # answered/closed; the binding stays for follow-ups
    return False


def _bg_llm_create(llm_client, result_container, **kwargs):
    """_capped_llm_create for call threads: marks the call in flight (the idle deadline waits
    for it, B4). Once promoted, a failed call is reported to the route service (route_failure,
    FAILURE_CLIENT) and the branch does what the answer says: retry (after wait_s; in parts after a
    long step), reroute (the rest of the request on the configured chain), stop or agent (raise).
    Before promotion the foreground's main loop reports its own call's failure."""
    while True:
        kwargs = route_call_kwargs(result_container.get("route_key"), kwargs)  # HIVE_ROUTE: this attempt's header
        result_container["llm_inflight_since"] = attempt_started = time.time()
        try:
            with _long_step_heartbeat(result_container.get("heartbeat_ids")):  # LONG_STEP (2)
                return _capped_llm_create(llm_client, **kwargs)
        except Exception as error:
            result_container.pop("llm_inflight_since", None)
            if not result_container.get("branch_id") or result_container.get("abandoned"):
                raise
            elapsed = time.time() - attempt_started
            retries = result_container.setdefault("retries", {})
            verdict = route_failure(result_container.get("route_key"), error, elapsed, retries)
            action = verdict["action"]
            print(f"BG_LLM_FAILURE: branch_id={result_container.get('branch_id')} action={action} "
                  f"class={verdict.get('class')} local={bool(verdict.get('local'))} "
                  f"error={type(error).__name__}: {str(error)[:200]}")
            if action == "retry":
                FailureClient.count(retries, verdict)
                if verdict.get("hint") == "continue_in_parts":  # LONG_STEP (1): the agent's own note
                    kwargs = dict(kwargs, messages=list(kwargs["messages"]) + [_long_step_note(elapsed)])
                wait = float(verdict.get("wait_s") or 0)
                result_container["last_progress_at"] = time.time() + wait  # retry = still alive
                time.sleep(wait)
                if result_container.get("abandoned"):
                    raise
                continue
            if action == "reroute":
                continue  # route_failure dropped the header: the configured chain answers the rest
            if action == "stop":
                result_container["stop_reason"] = verdict.get("reason")
            raise
        finally:
            result_container.pop("llm_inflight_since", None)


# --- per-request step accounting for branches (VM2 PROGRESS_NOT_PAUSE semantics) ---
class StepGuard:
    """The foreground's per-request counters (main loop) as an object a branch can fork.

    Same thresholds and texts as the foreground block in the main loop. record_step()
    returns ("nop", None), ("stop", reason), ("nudge"|"warn", note for the model),
    ("fallback", note Iter posts itself) or (None, None).
    """

    def __init__(self):
        self.steps = 0
        self.steps_since_send = HARD_STEP_CEILING + 1
        self.nudge_step = None
        self.signatures = []
        self.seen = set()
        self.stale = 0
        self.tally = {}
        self.answered = False

    def fork(self):
        return copy.deepcopy(self)

    def record_step(self, tool_calls, results):
        """results: [(tool_name, raw_arguments, return_text_as_recorded)] for this step."""
        self.steps += 1
        sent, digests = False, []
        for name, raw_arguments, ret in results:
            self.tally[name] = self.tally.get(name, 0) + 1
            ret = str(ret)
            if name == "send" and ret == "SUCCESS":
                self.answered = sent = True
            if name == "progress" and ret == "SUCCESS":
                sent = True
            digests.append(hashlib.sha256((str(name) + "\x00" + str(raw_arguments) + "\x00" + ret)
                                          .encode("utf-8", errors="replace")).hexdigest())
        self.steps_since_send = 0 if sent else self.steps_since_send + 1
        called_nop = any(call.function.name == "nop" for call in tool_calls)
        self.signatures = (self.signatures + [step_signature(tool_calls)])[-REPEAT_STEPS:]
        new_results = [digest for digest in digests if digest not in self.seen]
        self.seen.update(digests)
        self.stale = 0 if (new_results or not digests) else self.stale + 1
        if sent:
            self.nudge_step = None
        if called_nop:
            return "nop", None
        if self.steps >= HARD_STEP_CEILING:
            return "stop", f"I reached my per-request step ceiling ({HARD_STEP_CEILING} steps)"
        if len(self.signatures) >= REPEAT_STEPS and len(set(self.signatures)) == 1:
            return "stop", (f"my last {REPEAT_STEPS} steps repeated the same tool call with the same "
                            "arguments (possible loop)")
        if self.stale >= STALL_STEPS:
            return "stop", f"my last {STALL_STEPS} steps produced no new results (possible loop)"
        if self.steps % PROGRESS_EVERY == 0 and self.steps_since_send >= 3 and self.nudge_step is None:
            self.nudge_step = self.steps
            return "nudge", (
                f"[PROGRESS CHECKPOINT: you are at step {self.steps} of this "
                f"request (runaway ceiling {HARD_STEP_CEILING}) and have not updated the user in the last few "
                "steps. In your next step, call progress with a concise interim note (done / doing next / "
                "blockers; no secrets), then KEEP WORKING on the task. Do not stop, do not ask the user to "
                "type 'continue', and do not use send for this note unless it is your reply or final result. "
                "Pin key state if useful.]")
        if self.nudge_step is not None and self.steps - self.nudge_step >= PROGRESS_FALLBACK_STEPS:
            self.nudge_step = None
            return "fallback", (f"Still working on this (step {self.steps}); not paused, I will keep going. "
                                f"Actions so far: {_tally_text(self.tally)}.")
        if self.steps == HARD_STEP_CEILING - 2 and self.steps_since_send >= 3:
            return "warn", (
                f"[STEP CEILING WARNING: you are at step {self.steps} of the "
                f"{HARD_STEP_CEILING}-step runaway ceiling for this request. Within your next step, call send "
                "with a concise summary (done / remaining / current state; no secrets) and pin the key state "
                "so you can resume. The user can reply 'continue' to resume.]")
        return None, None


def _foreground_guard_snapshot(event_step):
    """StepGuard holding the foreground's counters just before a model call (main thread)."""
    guard = StepGuard()
    guard.steps = 0 if event_step else autonomous_steps + 1
    guard.steps_since_send = steps_since_send
    guard.nudge_step = progress_nudge_step
    guard.signatures = list(step_signatures)
    guard.seen = set(seen_step_results)
    guard.stale = stale_steps
    guard.tally = dict(tool_tally)
    guard.answered = answered_this_event
    return guard


_foreground_step_guard = None  # set by the main loop before each threaded call; a promoted branch forks it

# --- text-only endings (B7) ---
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


def _fallback_channel():
    name = os.getenv("ITER_CHECKPOINT_CHANNEL", "")
    if name:
        return name
    stems = [path.stem for path in sorted(Path("channels").glob("*.py")) if not path.name.startswith("_")]
    return stems[0] if stems else "terminal"


def send_text_only_fallback(content, model_tag=""):
    """Post a text-only reply that would otherwise be dropped (bound send). Returns True on success."""
    text = unwrap_pseudo_send((content or "").strip()).strip()
    if not text:
        print("TEXT_ONLY_FALLBACK_SKIPPED: empty")
        return False
    if len(text) > TEXT_ONLY_FALLBACK_MAX_CHARS:
        text = text[:TEXT_ONLY_FALLBACK_MAX_CHARS] + " [...truncated]"
    if model_tag and not text.startswith(model_tag):
        text = model_tag + "\n" + text
    try:
        result = invoke_dynamic(Path("tools/send.py"), "run", channel=_fallback_channel(), content=text)
    except Exception as error:
        print(f"TEXT_ONLY_FALLBACK_EXCEPTION: {type(error).__name__}: {error}")
        return False
    if not result.get("ok") or result.get("result") != "SUCCESS":
        print(f"TEXT_ONLY_FALLBACK_SEND_FAILED: {result}")
        return False
    print(f"TEXT_ONLY_FALLBACK_SENT: chars={len(text)}")
    return True


# --- thread visibility (C14) ---
SIBLING_SEND_WINDOW = _env_int("ITER_SIBLING_SEND_WINDOW", 1800, 0)
SIBLING_SNIPPET_CHARS = 300
_sibling_lock = threading.Lock()
_branch_activity = {}   # branch_id -> {"request", "started", "done_at", "inbox"}
_recent_sends = []      # [(time, origin, snippet)], newest last, bounded
_last_inbound = None    # (time, snippet) of the newest user message


def _snippet(text, limit=SIBLING_SNIPPET_CHARS):
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
    with _sibling_lock:
        request = _last_inbound[1] if _last_inbound else "(no user message)"
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
    """Other threads' tasks and recent sends, or '' (self_id: a branch id, None = foreground)."""
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


# --- branch registry ---
_branch_lock = threading.Lock()
_active_branches = {}  # branch_id -> BranchState; at most ITER_MAX_BACKGROUND_BRANCHES


def background_branch_active():
    with _branch_lock:
        return bool(_active_branches)


def background_branch_count():
    with _branch_lock:
        return len(_active_branches)


class BranchState:
    """A promoted background call (R11: deep-copied messages, R12: its own client)."""

    def __init__(self, branch_id, branch_client, branch_messages, thread, result_container, request_ids=None):
        self.branch_id = branch_id
        self.branch_client = branch_client
        self.branch_messages = branch_messages
        self.thread = thread
        self.result_container = result_container
        self.created_at = time.time()
        self.completed = False
        self.request_ids = dict(request_ids or {})  # channel -> request this branch answers


def _release_branch(branch_id, result_container, reason):
    """Free a branch's slot once and ask the main thread to close any request it left open."""
    result_container["finished"] = True
    _mark_branch_activity_done(branch_id)
    with _branch_lock:
        branch = _active_branches.pop(branch_id, None)
    if branch is None:
        return False
    route_episode_end(result_container.get("route_key"), {  # HIVE_ROUTE: the branch finished its request's turn
        "complete": None if result_container.get("answered") else "stalled",
        "error": "outage"}.get(reason, "interrupted"))
    if branch.request_ids:
        _merge_queue.put({"_branch_done": True, "branch": branch_id, "reason": reason,
                          "request_ids": dict(branch.request_ids),
                          "stop_reason": result_container.get("stop_reason")})  # FAILURE_CLIENT
    return True


def _close_branch_requests(done):
    """Main thread: close requests a finished branch left open (no-op if it answered).
    A branch that died on a provider error closes with VM2's provider-failure text (D3)."""
    note = provider_stop_text(done.get("stop_reason")) if done.get("reason") == "error" else None
    for channel, rid in sorted((done.get("request_ids") or {}).items()):
        if _foreground_request_ids.get(channel) == rid:
            continue  # the foreground owns it again
        path = Path("channels") / (channel + ".py")
        result = invoke_dynamic(path, "close_active", *((note,) if note else ()), request_id=rid)
        if result.get("ok") and result.get("result"):
            print(f"BRANCH_REQUEST_CLOSED: branch_id={done.get('branch')} reason={done.get('reason')} request={rid[:12]}")
        elif not result.get("ok"):
            print(f"BRANCH_REQUEST_CLOSE_FAILED: branch_id={done.get('branch')} request={rid[:12]} error={result.get('error')}")


def _marker(branch_id, text, **fields):
    """A note for the shared history (user role: VM2's gateway folds system messages into the system prompt)."""
    return {"role": "user", "content": "Step " + get_current_time() + ": " + text, "branch": branch_id, **fields}


def _branch_error(branch_id, result_container, error):
    """R14 + D3: record the error and free the slot; the main thread closes the request with
    the provider-failure text. Not re-raised into the foreground (VM2 idle is provider-silent)."""
    _merge_queue.put(_marker(branch_id, f"[BACKGROUND_BRANCH_ERROR] branch_id={branch_id} "
                                        f"error={type(error).__name__}: {str(error)[:500]}",
                             error=True, error_type=type(error).__name__))
    _release_branch(branch_id, result_container, "error")
    print(f"BACKGROUND_ERROR: branch_id={branch_id} error={type(error).__name__}: {error}")


def _promotion_lock(result_container):
    """The per-call lock that orders "call finished" against "call promoted" (F2)."""
    return result_container.setdefault("promotion_lock", threading.Lock())


def _bg_llm_thread_target(llm_client, model, messages, tools, tool_choice, max_tokens, extra_body, result_container):
    """Call thread: one model call; if it was promoted meanwhile, it becomes the branch."""
    started = time.time()
    try:
        response = _bg_llm_create(
            llm_client, result_container, model=model, messages=messages, tools=tools,
            tool_choice=tool_choice, max_tokens=max_tokens, extra_body=extra_body,
        )
        # F2: outcome and branch_id are read/written under promotion_lock, so the call either
        # finished before promotion (the foreground keeps it) or the branch is fully set up.
        with _promotion_lock(result_container):
            result_container["ok"] = True
            result_container["response"] = response
            result_container["last_progress_at"] = time.time()
            branch_id = result_container.get("branch_id")
    except Exception as error:
        with _promotion_lock(result_container):
            result_container["ok"] = False
            result_container["error"] = f"{type(error).__name__}: {error}"
            result_container["exception"] = error  # F3/K9: the caller re-raises the original
            branch_id = result_container.get("branch_id")
        if branch_id:
            print(f"LLM_CALL_SECONDS {time.time() - started:.1f} outcome=error branch_id={branch_id}")
            _branch_error(branch_id, result_container, error)
        return
    if not branch_id:
        return
    print(f"LLM_CALL_SECONDS {time.time() - started:.1f} outcome=ok branch_id={branch_id}")
    if result_container.get("abandoned"):
        print(f"BACKGROUND_ABANDONED_RESULT_DROPPED: branch_id={branch_id}")
        return
    try:
        _bg_branch_mini_loop(branch_id, result_container["branch_client"], result_container["branch_messages"],
                             response, result_container)
    except Exception as error:  # a bug in the loop must still free the slot and close the request
        _merge_queue.put(_marker(branch_id, f"[BACKGROUND_BRANCH_CRASHED] branch_id={branch_id} "
                                            f"error={type(error).__name__}: {str(error)[:500]}", error=True))
        _release_branch(branch_id, result_container, "crash")
        print(f"BRANCH_CRASHED: branch_id={branch_id} error={type(error).__name__}: {error}")


def _trim_branch_messages(messages):
    """Shorten old tool results and drop old reasoning, like the foreground's history clean-up."""
    for message in messages[:-RETURN_VALUE_PRESERVE_MESSAGES]:
        if (message.get("role") == "tool" and isinstance(message.get("content"), str)
                and len(message["content"]) > RETURN_VALUE_PRESERVE
                and not message["content"].endswith(" [TRUNCATED]")):
            message["content"] = message["content"][:RETURN_VALUE_PRESERVE] + " [TRUNCATED]"
        for key in ("reasoning", "reasoning_details", "reasoning_content"):
            message.pop(key, None)


def _bg_branch_mini_loop(branch_id, branch_client, branch_messages, initial_response, result_container):
    """A promoted branch answers its own request with its own tool loop.

    Same rules as the foreground: tool_choice="required"; progress-not-pause (forked
    StepGuard); nop without an answer -> "nothing to add" notice; runaway backstop ->
    step-limit notice; every channel write bound to the branch's request.  History
    entries go to the main thread through _merge_queue (single writer).
    """
    if result_container.get("request_ids") is not None:
        _tool_context.request_ids = dict(result_container["request_ids"])
    response = initial_response
    branch_steps = 0
    guard = result_container.get("step_guard") or StepGuard()
    stop_reason, branch_note = None, None
    inops, _omitted, _tool_errors = load_tools()
    tools = native_tools(inops)
    while branch_steps < BRANCH_STEP_BUDGET:
        if result_container.get("abandoned"):
            print(f"BRANCH_STOPPED_AFTER_ABANDON: branch_id={branch_id} steps={branch_steps}")
            return
        message = response.choices[0].message
        model_tag = served_model_tag(response)
        route_served(result_container.get("route_key"), response)  # HIVE_ROUTE
        raw_content = message.content
        if message.content:
            message.content += "\n[NOT DELIVERED TO ANY CHANNEL. IF THIS WAS INTENDED AS COMMUNICATION, USE send.]"
        if message.tool_calls:
            message.tool_calls = message.tool_calls[:MAX_TOOL_CALLS]
        assistant_entry = {key: value for key, value in message.model_dump(exclude_none=True).items()
                           if KEEP_REASONING_IN_EPISODE or key not in ("reasoning", "reasoning_details", "reasoning_content")}
        _merge_queue.put({**assistant_entry, "branch": branch_id})
        branch_messages.append(assistant_entry)
        if not message.tool_calls:
            # B7: a text-only ending of an unanswered request is delivered, not dropped.
            if not guard.answered and not result_container.get("abandoned"):
                if send_text_only_fallback(raw_content, model_tag):
                    guard.answered = True
                    print(f"BRANCH_TEXT_ONLY_FALLBACK_SENT: branch_id={branch_id}")
            break
        step_results = []
        for tool_call in message.tool_calls:
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
                        if (tool_name in ("send", "progress") and model_tag and isinstance(tool_arguments.get("content"), str)
                                and tool_arguments["content"] and not tool_arguments["content"].startswith(model_tag)):
                            tool_arguments = dict(tool_arguments, content=model_tag + "\n" + tool_arguments["content"])
                        result = invoke_dynamic(inops[tool_name][0], "run", **tool_arguments)
                        ret = result["result"] if result["ok"] else f"Tool execution failed: {result['error']}"
                except Exception as error:
                    ret = f"Tool execution failed: {type(error).__name__}: {error}"
            ret = str(ret)
            if tool_name == "send" and ret == "SUCCESS" and isinstance(tool_arguments, dict):
                _note_send(branch_id, tool_arguments.get("content"))
            if len(ret) > MAX_TOOL_OUTPUT_CHARS:
                ret = ret[:MAX_TOOL_OUTPUT_CHARS] + " [TRUNCATED]"
            step_results.append((tool_name, tool_call.function.arguments, ret))
            tool_content = "Step " + get_current_time() + ": " + ret
            _merge_queue.put({"role": "tool", "tool_call_id": tool_call.id, "content": tool_content, "branch": branch_id})
            branch_messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_content})
            result_container["last_progress_at"] = time.time()
        branch_steps += 1
        action, text = guard.record_step(message.tool_calls, step_results)
        if action == "nop":
            if not guard.answered:
                publish_no_response_notice()
            print(f"BRANCH_NOP: branch_id={branch_id} steps={guard.steps} answered={guard.answered}")
            break
        if action == "stop":
            stop_reason = text
            break
        if action in ("nudge", "warn"):
            branch_note = text
            print(f"BRANCH_PROGRESS_{action.upper()}: branch_id={branch_id} step={guard.steps}")
        elif action == "fallback":
            delivered = publish_progress_note(text)
            print(f"BRANCH_PROGRESS_FALLBACK: branch_id={branch_id} step={guard.steps} delivered={delivered}")
        inbox = take_branch_inbox(branch_id)
        if inbox:
            branch_messages.append({"role": "user", "content": inbox})
            print(f"BRANCH_INBOX_DELIVERED: branch_id={branch_id}")
        if branch_note:
            branch_messages.append({"role": "user", "content": "Step " + get_current_time() + ": " + branch_note})
            branch_note = None
        _trim_branch_messages(branch_messages)
        view = sibling_view(branch_id)
        call_messages = branch_messages + ([{"role": "user", "content": view}] if view else [])
        call_started = time.time()
        try:
            response = _bg_llm_create(
                branch_client, result_container, model=MODEL, messages=call_messages, tools=tools,
                tool_choice="required", max_tokens=MAX_TOKENS, extra_body={"enable_thinking": True},
            )
        except Exception as error:
            print(f"LLM_CALL_SECONDS {time.time() - call_started:.1f} outcome=error branch_id={branch_id}")
            _branch_error(branch_id, result_container, error)
            return
        print(f"LLM_CALL_SECONDS {time.time() - call_started:.1f} outcome=ok branch_id={branch_id}")
        result_container["last_progress_at"] = time.time()
    if stop_reason is None and branch_steps >= BRANCH_STEP_BUDGET:
        stop_reason = f"I reached my per-request step ceiling ({HARD_STEP_CEILING} steps)"
    if stop_reason is not None:
        print(f"BRANCH_BACKSTOP: branch_id={branch_id} steps={guard.steps} reason={stop_reason}")
        publish_step_limit_notice(guard.steps, guard.tally, stop_reason)
        _merge_queue.put({"_paused_at_limit": True, "branch": branch_id})
    result_container["answered"] = guard.answered and stop_reason is None  # HIVE_ROUTE: episode class
    _release_branch(branch_id, result_container, "complete")
    print(f"BRANCH_COMPLETE: branch_id={branch_id} steps={branch_steps}")


# PROMOTE_WHEN_WAITING (2026-10-14; Andy: "the purpose of multi thread is that the agent does not get
# blocked and can handle other tasks/questions"). A call used to move to a background branch only after
# ITER_PROMOTE_SECONDS, so a long task made of short steps kept the foreground and a new request waited
# until the whole task finished. Now a call is also promoted as soon as a new request is waiting in the
# channel inbox and could be claimed: the task continues in a branch, the foreground takes the request.
ITER_PROMOTE_POLL_SECONDS = 0.5


def _input_waiting():
    """True when a new request is waiting in the outer channel's inbox (request binding on)."""
    root = os.environ.get("ITER_OUTER_CHANNEL_ROOT")
    if not (ITER_REQUEST_BINDING and root):
        return False
    try:
        return any(Path(root, "inbox").glob("*.json"))
    except OSError:
        return False


def _room_for_early_promotion():
    """A branch slot is free, and after promotion the foreground could still claim a request: the
    promoted call keeps its request open, so the open-request cap needs room for one more."""
    branches = background_branch_count()
    if branches >= ITER_MAX_BACKGROUND_BRANCHES:
        return False
    try:
        open_cap = int(os.environ.get("ITER_MAX_OPEN_REQUESTS") or 0)
    except ValueError:
        open_cap = 0
    return not open_cap or branches + 2 <= open_cap


def _join_or_promote_early(thread):
    """Wait for a model call up to ITER_PROMOTE_SECONDS; stop waiting as soon as a new request is
    waiting and could be claimed (checked at the call's start and every ITER_PROMOTE_POLL_SECONDS),
    so the caller promotes the call now."""
    deadline = time.time() + ITER_PROMOTE_SECONDS
    while thread.is_alive():
        if _input_waiting() and _room_for_early_promotion():
            print("LLM_PROMOTE_EARLY: a new request is waiting; this call moves to a background branch now")
            return
        left = deadline - time.time()
        if left <= 0:
            return
        thread.join(timeout=min(ITER_PROMOTE_POLL_SECONDS, left))
# END PROMOTE_WHEN_WAITING


def threaded_llm_call(llm_client, model, messages, tools, tool_choice, max_tokens, extra_body):
    """Model call with promotion deadline ITER_PROMOTE_SECONDS.

    Done in time -> (response, None), exactly like a direct call. Still running -> promoted
    to a background branch (deep-copied messages, its own client, the foreground's request
    binding and step count) -> (None, BranchState). Branch set full -> wait in the foreground.
    """
    result_container = {"heartbeat_ids": dict(_foreground_request_ids) if ITER_REQUEST_BINDING else None,
                        "route_key": _route_fg_key,  # HIVE_ROUTE: the call (and a branch) keeps its request's episode
                        "retries": _fg_retries}  # FAILURE_CLIENT: and its retry counts
    _promotion_lock(result_container)
    thread = threading.Thread(
        target=_bg_llm_thread_target,
        args=(llm_client, model, messages, tools, tool_choice, max_tokens, extra_body, result_container),
        daemon=True,
    )
    thread.start()
    _wait = globals().get("_join_or_promote_early")  # PROMOTE_WHEN_WAITING (absent when a test execs this alone)
    if _wait:
        _wait(thread)
    else:
        thread.join(timeout=ITER_PROMOTE_SECONDS)

    if thread.is_alive() and background_branch_count() >= ITER_MAX_BACKGROUND_BRANCHES:
        # Never evict a running branch: answer this call in the foreground instead.
        print(f"LLM_PROMOTION_SKIPPED: {ITER_MAX_BACKGROUND_BRANCHES} background branch(es) already active; waiting in foreground")
        thread.join(timeout=BG_INFLIGHT_LIMIT)
        if thread.is_alive():
            raise Exception("Background LLM call failed: APITimeoutError: foreground call exceeded BG_INFLIGHT_LIMIT")

    promoted = False
    if thread.is_alive():
        branch_id = f"bg-{uuid.uuid4().hex[:8]}"
        request_ids = dict(_foreground_request_ids) if ITER_REQUEST_BINDING else {}
        with _promotion_lock(result_container):
            if "ok" not in result_container:  # else the call finished meanwhile: keep it in the foreground
                promoted = True
                result_container["request_ids"] = request_ids if ITER_REQUEST_BINDING else None
                fg_guard = globals().get("_foreground_step_guard")
                if fg_guard is not None and hasattr(fg_guard, "fork"):
                    result_container["step_guard"] = fg_guard.fork()
                bg_messages = copy.deepcopy(messages)
                bg_client = openai.OpenAI(
                    api_key=API_KEY, base_url=BASE_URL,
                    timeout=LLM_TIMEOUT, max_retries=0,
                    default_headers={"X-APC-Tenant": "iter-bg", "x-session-id": SESSION_ID, "x-branch-id": branch_id},
                )
                result_container["branch_client"] = bg_client
                result_container["branch_messages"] = bg_messages
                result_container["branch_id"] = branch_id  # published last (F2)
        if not promoted:
            print("LLM_PROMOTION_RACE_AVOIDED: call finished at the deadline; answered in the foreground")
    if promoted:
        branch = BranchState(
            branch_id=branch_id, branch_client=bg_client, branch_messages=bg_messages,
            thread=thread, result_container=result_container, request_ids=request_ids,
        )
        _register_branch_activity(branch_id)
        with _branch_lock:
            _active_branches[branch_id] = branch
            active_count = len(_active_branches)
        if result_container.get("finished"):  # finished before it was registered
            _release_branch(branch_id, result_container, "complete")
        if ITER_REQUEST_BINDING:
            _foreground_request_ids.clear()  # the branch owns these requests now
            _foreground_open.clear()
        print(f"LLM_PROMOTED: branch_id={branch_id} deadline={ITER_PROMOTE_SECONDS}s "
              f"active={active_count}/{ITER_MAX_BACKGROUND_BRANCHES} "
              f"requests={sorted(rid[:12] for rid in request_ids.values())}")
        return None, branch

    if result_container.get("ok"):
        return result_container["response"], None
    original = result_container.get("exception")
    if original is not None:
        raise original  # F3/K9: keep type, status_code and response headers
    raise Exception(f"Background LLM call failed: {result_container.get('error', 'unknown')}")


# --- merge queue: branches -> main thread (R15 double drain, R16 tags, single writer) ---
_merge_queue = queue.Queue()


def _build_tool_call_map(messages):
    lookup = {}
    for entry in messages:
        if entry.get("role") == "assistant" and entry.get("tool_calls"):
            for tc in entry["tool_calls"]:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    lookup[tc.get("id")] = (fn.get("name", ""), fn.get("arguments", ""))
    return lookup


def drain_merge_queue():
    """Main thread only: append queued branch entries to experience (tagged), close
    requests finished branches left open, note a branch backstop. Returns entries merged."""
    global paused_at_limit
    merged_entries, done_payloads, paused = [], [], False
    while True:
        try:
            entry = _merge_queue.get_nowait()
        except queue.Empty:
            break
        if not isinstance(entry, dict):
            continue
        if entry.get("_branch_done"):
            done_payloads.append(entry)
            continue
        if entry.get("_paused_at_limit"):
            paused = True
            continue
        if "branch" not in entry:
            entry["branch"] = "unknown"
        merged_entries.append(entry)
    if paused:
        paused_at_limit = True  # the next event gets the "resume, don't restart" note
    for done in done_payloads:
        _close_branch_requests(done)
    if not merged_entries:
        return 0
    merged_call_lookup = _build_tool_call_map(merged_entries)
    existing_call_lookup = _build_tool_call_map(experience)
    existing_tool_index = {}
    for i, entry in enumerate(experience):
        if entry.get("role") != "tool":
            continue
        tc_id = entry.get("tool_call_id")
        if tc_id and tc_id in existing_call_lookup:
            existing_tool_index.setdefault(existing_call_lookup[tc_id], i)
    for entry in merged_entries:
        if entry.get("role") != "tool":
            continue
        tc_id = entry.get("tool_call_id")
        if tc_id and tc_id in merged_call_lookup and merged_call_lookup[tc_id] in existing_tool_index:
            entry["superseded_by"] = str(existing_tool_index[merged_call_lookup[tc_id]])
    for entry in merged_entries:
        experience.append(entry)
    save_experience(experience)
    print(f"MERGE_DRAINED: {len(merged_entries)} entries from background branch(es)")
    return len(merged_entries)


def check_background_deadline():
    """Abandon every branch idle (no model response or tool result) longer than
    BACKGROUND_DEADLINE, unless its call is still in flight within BG_INFLIGHT_LIMIT (B3/B4)."""
    now = time.time()
    abandoned = []
    with _branch_lock:
        for branch_id, branch in list(_active_branches.items()):
            elapsed = now - branch.created_at
            last_progress = branch.result_container.get("last_progress_at", branch.created_at)
            idle = now - last_progress
            if idle <= BACKGROUND_DEADLINE:
                continue
            inflight_since = branch.result_container.get("llm_inflight_since")
            if inflight_since is not None and now - inflight_since <= BG_INFLIGHT_LIMIT:
                continue
            has_result = bool(branch.result_container.get("ok", False))
            branch.result_container["abandoned"] = True  # tells the branch thread to stop
            branch.result_container["finished"] = True
            del _active_branches[branch_id]
            abandoned.append((branch, elapsed, idle, has_result))
    for branch, elapsed, idle, has_result in abandoned:
        branch_id = branch.branch_id
        route_episode_end(branch.result_container.get("route_key"), "stalled")  # HIVE_ROUTE
        _mark_branch_activity_done(branch_id)
        _merge_queue.put(_marker(branch_id, f"[BACKGROUND_BRANCH_ABANDONED] branch_id={branch_id} "
                                            f"elapsed={elapsed:.1f}s idle={idle:.1f}s deadline={BACKGROUND_DEADLINE}s. "
                                            f"LLM call completed: {has_result}. Results discarded.", abandoned=True))
        if branch.request_ids:
            _merge_queue.put({"_branch_done": True, "branch": branch_id, "reason": "abandoned",
                              "request_ids": dict(branch.request_ids)})
        print(f"BACKGROUND_ABANDONED: branch_id={branch_id} elapsed={elapsed:.1f}s idle={idle:.1f}s "
              f"deadline={BACKGROUND_DEADLINE}s llm_completed={has_result}")
    return bool(abandoned)


def _owned_request_ids():
    """Request ids a live thread still owns: the foreground's open request and every active branch's."""
    owned = {rid for stem, rid in _foreground_request_ids.items() if stem in _foreground_open}
    with _branch_lock:
        for branch in _active_branches.values():
            owned.update(branch.request_ids.values())
    return owned


def expire_stale_requests(now=None):
    """Main thread, at most every EXPIRE_CHECK_INTERVAL s (also while branches work): close
    ORPHANED open requests (no live owner) with no claim/progress activity for ITER_REQUEST_TIMEOUT.
    Requests the foreground or a branch still owns are kept: their owner answers or closes them,
    and a branch's own deadlines (idle, in-flight, step ceiling) bound how long that takes."""
    global _last_expire_check
    now = time.time() if now is None else now
    if ITER_REQUEST_TIMEOUT <= 0 or now - _last_expire_check < EXPIRE_CHECK_INTERVAL:
        return False
    _last_expire_check = now
    closed = False
    keep = sorted(_owned_request_ids())
    for path in sorted(Path("channels").glob("*.py")):
        if path.name.startswith("_") or not _channel_has(path, "expire_stale"):
            continue
        result = invoke_dynamic(path, "expire_stale", ITER_REQUEST_TIMEOUT, keep=keep)
        if result.get("ok") and result.get("result") is True:
            print(f"STALE_REQUEST_EXPIRED: channel={path.stem}")
            closed = True
        elif not result.get("ok"):
            print(f"STALE_REQUEST_EXPIRE_FAILED: channel={path.stem} error={result.get('error')}")
    return closed


# --- shutdown (R17): SIGTERM/SIGINT -> stop event -> short grace -> drain -> save -> exit ---
_shutdown_event = threading.Event()


def _iter_signal_handler(signum, frame):
    try:
        sig_name = signal.Signals(signum).name
    except (AttributeError, ValueError):
        sig_name = str(signum)
    print(f"SHUTDOWN_SIGNAL: {sig_name} received, setting shutdown event")
    _shutdown_event.set()


def _install_signal_handlers():
    try:
        signal.signal(signal.SIGTERM, _iter_signal_handler)
        signal.signal(signal.SIGINT, _iter_signal_handler)
        print("SIGNAL_HANDLERS_INSTALLED: SIGTERM, SIGINT")
    except (ValueError, OSError) as error:
        print(f"SIGNAL_HANDLER_INSTALL_FAILED: {type(error).__name__}: {error}")


def graceful_shutdown():
    """Wait up to SHUTDOWN_GRACE s for branch calls to return, drain, save, exit.
    Branch threads are daemons; their requests are released by the supervisor at restart."""
    print(f"SHUTDOWN: grace={SHUTDOWN_GRACE}s")
    deadline = time.time() + SHUTDOWN_GRACE
    while time.time() < deadline:
        with _branch_lock:
            branches = list(_active_branches.values())
        if all(branch.result_container.get("ok") is not None for branch in branches):
            break
        time.sleep(0.1)
    drained = drain_merge_queue()
    save_experience(experience)
    print(f"SHUTDOWN_COMPLETE: drained={drained} experience_len={len(experience)}")
    sys.exit(0)
# END CONCURRENCY_PORT machinery

# BEGIN LONG_STEP (2026-10-13, Andy): a model call that runs long is work in progress, not an
# outage. (1) The first timeout of a step is not a provider failure: the step is retried once
# with a note asking the model to continue in smaller parts (foreground and branches). Since
# 2026-10-23 the route service decides that a failure is a long step (/episodes/failure answers
# retry with hint continue_in_parts); Iter keeps only the note's wording. (2) A call still running
# after ITER_LONG_STEP_NOTICE_SECONDS posts ONE mechanical progress note to its request (no
# model call), so the person sees activity and the receiver keeps waiting.
LONG_STEP_NOTICE_SECONDS = _env_int("ITER_LONG_STEP_NOTICE_SECONDS", 240, 1)
LONG_STEP_NOTE = ("[YOUR LAST STEP RAN {seconds}s WITHOUT FINISHING (IT TIMED OUT OR WAS STOPPED WHILE THE MODEL WAS "
                  "STILL THINKING OR GENERATING). Continue the same task in smaller steps: post a short progress note "
                  "first, then finish the work in parts. Keep each message under about 3500 characters.]")


def _long_step_note(seconds):
    return {"role": "user", "content": "Step " + get_current_time() + ": " + LONG_STEP_NOTE.format(seconds=int(seconds))}


@contextlib.contextmanager
def _long_step_heartbeat(request_ids=None):
    """While a model call runs, post one progress note to its request after
    LONG_STEP_NOTICE_SECONDS. request_ids: the binding of the request the call works for
    (None = the original unbound contract)."""
    started = time.time()

    def notify():
        if request_ids is not None:
            _tool_context.request_ids = dict(request_ids)  # the timer thread answers that request only
        minutes = max(1, int((time.time() - started) // 60))
        delivered = publish_progress_note(
            f"Still working on a long step ({minutes} min so far); not stuck, I will keep going.")
        print(f"LONG_STEP_HEARTBEAT: after={time.time() - started:.0f}s delivered={delivered}")
    timer = threading.Timer(LONG_STEP_NOTICE_SECONDS, notify)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
# END LONG_STEP

# BEGIN HIVE_ROUTE: per-episode model routing (hive-route rev 13, ADOPTING.md §10), Protomega2's
# fixed-mode pilot (2026-10-06), per request since the concurrency port (2026-10-14,
# CONCURRENCY_PORT.md §10). An episode is one request. When the foreground claims a request, the hook
# asks the route service for a model (with the episode's text, for the router's reader) and every
# call made for that request, in the foreground or in a promoted branch, sends it as x-openclaw-model
# (which also turns off the gateway's own fallbacks). Whoever finishes the request reports the served
# models once. On a provider failure the episode ends at once and the rest of the request runs on the
# configured chain. Fail open: if the service can't be reached, answers anything but "route", or
# routed calls keep being rejected (breaker), requests run on the agent's configured chain (no
# header), as before routing, logged locally in HIVE_ROUTE_LOG (ids, models, errors; never text).
# Switch off: remove the token file (no restart needed). Nothing here ever raises into Iter.
# One file for both VM2 bots since 2026-10-23 (Protomega + Protomega2 merge): the per-bot settings
# come from the environment the supervisor sets. HIVE_ROUTE_TOKEN_FILE (unset = hook off) and
# HIVE_ROUTE_MODE: "apply" sends the routed model as above; "read" (the default, Protomega) asks
# the route service and reports served models the same way, but never sends the header and has no
# breaker, so the gateway's model chain and fallbacks are exactly as without the hook.
import urllib.request as _route_urllib
import uuid as _route_uuid
HIVE_ROUTE_URL = os.getenv("HIVE_ROUTE_URL", "http://172.18.0.1:8480")
HIVE_ROUTE_TOKEN_FILE = os.getenv("HIVE_ROUTE_TOKEN_FILE", "")  # per bot, from the supervisor; unset = hook off
HIVE_ROUTE_MODE = os.getenv("HIVE_ROUTE_MODE", "read").strip().lower()
if HIVE_ROUTE_MODE not in ("read", "apply"):
    print(f"HIVE_ROUTE_MODE {HIVE_ROUTE_MODE!r} unknown: using read")
    HIVE_ROUTE_MODE = "read"
HIVE_ROUTE_APPLY = HIVE_ROUTE_MODE == "apply"
# FAILURE_CLIENT: hive-route's examples/failure_client.py, copied verbatim beside iter.py (loaded by
# path, so it doesn't depend on sys.path; a missing file stops Iter at start, loudly).
_fc_spec = importlib.util.spec_from_file_location("failure_client", Path(__file__).resolve().with_name("failure_client.py"))
_fc_module = importlib.util.module_from_spec(_fc_spec)
_fc_spec.loader.exec_module(_fc_module)
FailureClient = _fc_module.FailureClient
_failure_client = FailureClient(HIVE_ROUTE_URL, token_file=HIVE_ROUTE_TOKEN_FILE or None,
                                fallback_wait_s=FAILURE_FALLBACK_WAIT)
print(f"HIVE_ROUTE mode={HIVE_ROUTE_MODE} token_file={'set' if HIVE_ROUTE_TOKEN_FILE else 'unset (hook off)'}")
HIVE_ROUTE_LOG = os.getenv("HIVE_ROUTE_LOG", "route-hook.jsonl")
HIVE_ROUTE_BREAKER = 2  # routed calls rejected in a row (not capacity/outage) before the hook switches off
HIVE_ROUTE_TEXT_CHARS = 12000  # the reader's max_chars (hive-route's default): the router reads no further
HIVE_ROUTE_ROUTE_TIMEOUT = 15  # /route waits for the reader (timeout_s 10 in the table) plus the state build
_route_lock = threading.Lock()
_route_episodes = {}  # request key -> {"task", "episode", "model", "served": set()}
_route_breaker = {"rejected": 0, "off": None}  # per agent (§10.6): describes the route service
_route_last = {"task": None, "objective": None}  # the latest task, for a "continue" after a pause
_route_fg_key = None  # the foreground's current episode key (None after promotion: the branch owns it)


def _route_note(event, **fields):
    """Local record of the hook's decisions: ids, models and errors only, never request text."""
    line = dict(fields, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), event=event)
    try:
        with _route_lock, open(HIVE_ROUTE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True, default=str) + "\n")
    except OSError:
        pass
    print("HIVE_ROUTE " + json.dumps(line, sort_keys=True, default=str))


def _route_token():
    try:
        with open(HIVE_ROUTE_TOKEN_FILE, encoding="ascii") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _route_post(path, body, token, timeout=5):
    req = _route_urllib.Request(HIVE_ROUTE_URL + path, data=json.dumps(body).encode(), method="POST",
                                headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with _route_urllib.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _route_tail(text, limit):
    text = str(text or "").strip()
    return text if len(text) <= limit else "[...] " + text[-(limit - 6):]


def _route_text(request, experience, objective=None):
    """The episode's objective and the context it rests on, never just the last message (hive-route
    ROUTING.md §4.8): the request as delivered (the Telegram bridge includes the recent chat lines), the
    task's first request when this episode continues a paused one, and this agent's latest replies. Each
    part keeps its newest end, and the whole fits HIVE_ROUTE_TEXT_CHARS, so the router never cuts it."""
    replies = []
    for message in reversed(experience or []):
        for call in (message.get("tool_calls") or []) if isinstance(message, dict) else []:
            function = (call.get("function") or {}) if isinstance(call, dict) else {}
            if function.get("name") != "send":
                continue
            try:
                content = json.loads(function.get("arguments") or "{}").get("content")
            except Exception:
                content = None
            if content:
                replies.append(_route_tail(content, 600))
        if len(replies) >= 3:
            break
    context = []
    if objective and objective != request:
        context.append("This episode continues a paused task. The task's first request:\n"
                       + _route_tail(objective, 3000))
    if replies:
        context.append("This agent's latest replies in the conversation, oldest first:\n"
                       + "\n---\n".join(reversed(replies[:3])))
    head = "Episode objective: the request as delivered, with the recent chat lines it came with:\n"
    rest = "".join("\n\n" + c for c in context)
    return head + _route_tail(request, HIVE_ROUTE_TEXT_CHARS - len(head) - len(rest)) + rest


def _route_key(request):
    """Episode key: a request id, or a binding dict ({channel: request_id}); "fg" when unbound."""
    if isinstance(request, str):
        return request
    ids = sorted((request or {}).values())
    return ids[0] if ids else "fg"


def route_episode_start(request, request_text, experience, continuing=False):
    """A newly claimed request starts its episode: /route once, with its text. Returns its key. A
    "continue" after a pause stays in the paused task (same task id, its first request in the text)."""
    key = _route_key(request)
    try:
        with _route_lock:
            if key in _route_episodes:
                return key  # this request's episode is already open
            last_task, last_objective = _route_last["task"], _route_last["objective"]
            off = _route_breaker["off"]
        continuing = bool(continuing and last_task)
        task = last_task if continuing else "req-" + _route_uuid.uuid4().hex[:24]
        objective = last_objective if continuing else request_text
        with _route_lock:
            _route_last["task"], _route_last["objective"] = task, objective
        if off:
            _route_note("fail_open", key=key[:12], task=task, reason="hook switched off: " + off)
            return key
        token = _route_token()
        if not token:
            return key  # hook not configured: run exactly as before routing
        episode = task + "-" + _route_uuid.uuid4().hex[:8]
        body = {"task": task, "episode": episode, "facts": {}}
        try:
            text = _route_text(request_text, experience, objective if continuing else None) if request_text else None
        except Exception as error:
            text = None
            _route_note("text_failed", key=key[:12], task=task, error=f"{type(error).__name__}: {error}"[:200])
        if text:
            body["text"] = text
        decision = _route_post("/route", body, token, timeout=HIVE_ROUTE_ROUTE_TIMEOUT)
        if decision.get("decision") == "route" and isinstance(decision.get("model"), str) and "/" in decision["model"]:
            with _route_lock:
                _route_episodes[key] = {"task": task, "episode": episode, "model": decision["model"], "served": set()}
            _route_note("routed", key=key[:12], task=task, episode=episode, model=decision["model"],
                        applied=HIVE_ROUTE_APPLY, route_id=decision.get("route_id"), tier=decision.get("tier"), continuing=continuing,
                        text_chars=len(text) if text else 0)
        else:
            # Not routed (wait / no_route / reconcile): the service recorded no episode, so no
            # /episodes/end. Fixed-mode pilot policy: fail open instead of waiting.
            _route_note("fail_open", key=key[:12], task=task, episode=episode,
                        reason="decision " + str(decision.get("decision")), wait_until=decision.get("wait_until"))
    except Exception as error:
        _route_note("fail_open", key=key[:12], reason=f"{type(error).__name__}: {error}"[:200])
    return key


def route_headers(request):
    """The header that pins a call made for this request to its episode's routed model; None when the
    request has no open routed episode (not routed, ended after a failure) or the breaker is open.
    Always None in read mode."""
    if not HIVE_ROUTE_APPLY:
        return None
    try:
        with _route_lock:
            episode = _route_episodes.get(_route_key(request)) if request is not None else None
            if episode and episode.get("model") and not _route_breaker["off"]:
                return {"x-openclaw-model": episode["model"]}
    except Exception:
        pass
    return None


def route_call_kwargs(request, kwargs):
    """kwargs for one call attempt: this request's routing header, or none (never a stale one)."""
    headers = route_headers(request)
    kwargs = {k: v for k, v in kwargs.items() if k != "extra_headers"}
    if headers:
        kwargs["extra_headers"] = headers
    return kwargs


def route_served(request, response):
    """Record the model that served a call made for this request (the gateway's served_model)."""
    try:
        served = getattr(response, "served_model", None)
        with _route_lock:
            episode = _route_episodes.get(_route_key(request)) if request is not None else None
            if episode is None:
                return
            if episode.get("model"):
                _route_breaker["rejected"] = 0  # only a routed call that succeeds clears the breaker count
            if isinstance(served, str) and served.strip():
                episode["served"].add(served.strip())
    except Exception:
        pass


def route_episode_end(request, cls=None):
    """End the request's episode exactly once (whoever finishes it first)."""
    if request is None:
        return
    key = _route_key(request)
    with _route_lock:
        episode = _route_episodes.pop(key, None)
    if episode is None:
        return
    try:
        token = _route_token()
        if not token:
            _route_note("end_skipped_no_token", key=key[:12], episode=episode["episode"])
            return
        body = {"episode": episode["episode"], "models": sorted(episode["served"])}
        if cls:
            body["class"] = cls
        answer = _route_post("/episodes/end", body, token)
        _route_note("ended", key=key[:12], episode=episode["episode"], cls=cls, models=body["models"],
                    drift=answer.get("drift"))
    except Exception as error:
        _route_note("end_failed", key=key[:12], error=f"{type(error).__name__}: {error}"[:200])


def route_failure(request, error, elapsed_s, retries):
    """A model call made for this request failed: ask the route service what to do (/episodes/failure,
    ROUTING.md §9.4) and return its verdict: {"action": retry|reroute|stop|agent, "class", "reason",
    "wait_s", "hint", ...}. The service owns the rules; Iter only acts on the answer.
    - In apply mode a call on a routed episode reports that episode and its served models. Anything but
      a retry then ends it at the service (no /episodes/end of ours); the header goes, so the rest of
      the request runs on the configured chain. Rejections of the override (4xx other than 429) count
      toward the per-agent breaker.
    - In read mode, or without a routed episode, the service only classifies (it never says reroute);
      a stop ends our episode with the verdict's class.
    - Service unreachable or no token: the client's local, cautious answer (one retry, then stop).
    - A wait for one of Iter's own model-call slots is never reported (it's not the provider's)."""
    if "concurrency cap" in str(error):
        return {"class": None, "action": "agent", "reason": "waited for a model-call slot", "local": True}
    key = _route_key(request) if request is not None else None
    try:
        with _route_lock:
            episode = _route_episodes.get(key) if key else None
            routed = bool(HIVE_ROUTE_APPLY and episode and episode.get("model") and not _route_breaker["off"])
            sent, models = (episode["episode"], sorted(episode["served"])) if routed else (None, None)
        verdict = _failure_client.report(error, elapsed_s=elapsed_s, retries=retries, episode=sent, models=models)
        if not isinstance(verdict, dict) or verdict.get("action") not in ("retry", "reroute", "stop", "agent"):
            verdict = _failure_client.local(retries)
        action, cls = verdict["action"], verdict.get("class")
        if action in ("reroute", "stop"):
            if routed and verdict.get("ended"):
                status = error_status(error)
                with _route_lock:
                    _route_episodes.pop(key, None)  # ended by the service, with its class
                    breaker_opened = None
                    if isinstance(status, int) and 400 <= status < 500 and status != 429:
                        _route_breaker["rejected"] += 1
                        if _route_breaker["rejected"] >= HIVE_ROUTE_BREAKER and not _route_breaker["off"]:
                            _route_breaker["off"] = (f"{_route_breaker['rejected']} routed calls rejected in a row: "
                                                     f"HTTP {status}")[:200]
                            breaker_opened = _route_breaker["off"]
                if breaker_opened:
                    _route_note("breaker_open", reason=breaker_opened)
            elif key:
                route_episode_end(key, cls or "outage")
        _route_note("failure", key=(key or "")[:12], action=action, cls=cls, routed=routed,
                    local=bool(verdict.get("local")), ended=verdict.get("ended"), wait_s=verdict.get("wait_s"),
                    hint=verdict.get("hint"), reason=str(verdict.get("reason"))[:200],
                    error=f"{type(error).__name__}: {error}"[:200])
        return verdict
    except Exception as error2:  # never raises into Iter: fall back to the client's local answer
        _route_note("failure_handling_failed", error=f"{type(error2).__name__}: {error2}"[:200])
        return _failure_client.local(retries)


def error_status(error):
    """The HTTP status of a failed call: the exception's, else one named in its message (as the service reads it)."""
    for candidate in (error, getattr(error, "response", None)):
        status = getattr(candidate, "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            return status
    match = re.search(r"\bError code:\s*(\d{3})\b", str(error))
    return int(match.group(1)) if match else None
# END HIVE_ROUTE

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
post_task_mode, autonomous_steps = False, 0
_fg_retries, _agent_errors = {}, 0  # FAILURE_CLIENT: this request's retries (sent with each report); non-provider call errors in a row
answered_this_event, paused_at_limit, tool_tally = False, False, {}
steps_since_send = HARD_STEP_CEILING + 1  # steps since the last successful send/progress (reset per event)
progress_nudge_step = None  # step at which the interim-progress nudge was injected
step_signatures, seen_step_results, stale_steps = [], set(), 0  # runaway backstop state (reset per event)
current_event_message = None
new_burst, pending_event_append = True, ""
admitted_event_fingerprints: list[tuple[str, float]] = []  # (fingerprint, admitted at)
cleanup_interval = MAX_EXPERIENCE_SIZE - RETAIN_EXPERIENCE_SIZE
cleanup_bucket = len(experience) // cleanup_interval
_long_step_pending = False  # LONG_STEP: seconds the last call ran when the service said "continue in parts"
_last_llm_failure = None  # FAILURE_CLIENT: (error, seconds, verdict) of the last failed foreground model call
if ITER_REQUEST_BINDING:  # CONCURRENCY_PORT: main-thread tools see the foreground's current binding
    _tool_context.request_ids = _foreground_request_ids
if ITER_CONCURRENCY_ENABLED:
    _install_signal_handlers()
    print(f"CONCURRENCY_ENABLED: branches={ITER_MAX_BACKGROUND_BRANCHES} "
          f"open_requests={os.environ.get('ITER_MAX_OPEN_REQUESTS')} llm_calls={ITER_MAX_CONCURRENT_LLM_CALLS} "
          f"promote={ITER_PROMOTE_SECONDS}s failures=route-service request_timeout={ITER_REQUEST_TIMEOUT:.0f}s")
while True:
    fire_due_alarms()
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
    # A long burst (~3.4 messages/step) can outgrow the history window, which
    # would drop the very request being worked on. Keep it pinned at the front.
    if current_event_message is not None and not any(m is current_event_message for m in experience):
        experience = [current_event_message] + experience
    if ITER_CONCURRENCY_ENABLED:  # CONCURRENCY_PORT housekeeping (main thread only)
        if _shutdown_event.is_set():
            graceful_shutdown()
        check_background_deadline()
        drain_merge_queue()  # (a) branch results before receive
        expire_stale_requests()
    history_checkpoint = len(experience) #before user input
    try:
        time.sleep(DEFAULT_DELAY)
        print("BEFORE RECEIVE")
        event_append = pending_event_append or receive()
        print("AFTER RECEIVE")
        if event_append:
            event_fingerprint = hashlib.sha256(event_append.encode("utf-8", errors="replace")).hexdigest()
            now = time.time()
            admitted_event_fingerprints = [(fingerprint, admitted_at) for fingerprint, admitted_at
                                           in admitted_event_fingerprints
                                           if now - admitted_at < DUPLICATE_WINDOW_SECONDS]
            if any(fingerprint == event_fingerprint for fingerprint, _ in admitted_event_fingerprints):
                # Every event from receive() is a claimed request: close it, or
                # processing/ stays occupied and the channel blocks all input.
                print("DUPLICATE EVENT SUPPRESSED " + event_fingerprint)
                publish_duplicate_notice()
                pending_event_append = slow_wait_for_input()
                continue
            admitted_event_fingerprints.append((event_fingerprint, now))
            admitted_event_fingerprints = admitted_event_fingerprints[-128:]
            route_episode_end(_route_fg_key, None if answered_this_event else "interrupted")  # HIVE_ROUTE
            _route_continuing = paused_at_limit  # HIVE_ROUTE: a "continue" after a pause is the same task
            autonomous_steps = 0
            _fg_retries, _agent_errors, _long_step_pending = {}, 0, False  # FAILURE_CLIENT: a new dict (a branch may hold the old one)
            new_burst, post_task_mode = False, False
            answered_this_event, tool_tally = False, {}
            steps_since_send = HARD_STEP_CEILING + 1
            progress_nudge_step = None
            step_signatures, seen_step_results, stale_steps = [], set(), 0
            if paused_at_limit:
                event_append = (
                    "[NOTE: your previous request was paused by a runaway backstop (step ceiling, "
                    "repeated identical steps, or no new results) and the "
                    "user was notified. If the message below asks you to continue, resume the same "
                    "task from your recent actions and pinned notes instead of restarting it, and "
                    "do not repeat work already done.]\n" + event_append)
                paused_at_limit = False
            print("IN FROM CHANNEL " + event_append)
            current_event_message = {"role": "user", "content": "Step " + get_current_time() + ": " + event_append}
            experience += [current_event_message]
            save_experience(experience)
            if ITER_CONCURRENCY_ENABLED:
                _note_inbound(event_append)
            _route_fg_key = route_episode_start(dict(_foreground_request_ids), event_append, experience,
                                                continuing=_route_continuing)  # HIVE_ROUTE
            pending_event_append = ""
            base_temporary_message = []
        elif new_burst or post_task_mode or autonomous_steps >= MAX_FAST_STEPS:
            # Idle is provider-silent.  The old post-task branch deliberately
            # called the model without an external event and, after nop, did
            # so forever.  It exhausted a shared Anthropic account on VM2.
            # Wait for an inbound event and return to the receive gate without
            # constructing a model request.
            new_burst, post_task_mode = True, True
            pending_event_append = slow_wait_for_input()
            continue
        else:
            base_temporary_message = [{"role": "user", "content": "Step " + get_current_time() + ": [NO ADDITIONAL USER INPUT. CONTINUE THE CURRENT USER TASK.]"}]
        history_checkpoint = len(experience) #as we want not to loose user input even when exception
        retry_message = None
        _promoted, _text_only_idle, _text_only_content, text_only_retries = False, False, None, 0
        while True:
            temporary_message = list(base_temporary_message)
            if _long_step_pending:  # LONG_STEP (1): the last call of this request ran long and failed
                temporary_message += [_long_step_note(_long_step_pending)]
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
            if ITER_CONCURRENCY_ENABLED and drain_merge_queue():  # (b) branch results this call should see
                history_checkpoint = len(experience)  # a failed call's roll-back keeps merged branch entries
            request_messages = [{"role": "system", "content": "prompt.txt:\n" + open("prompt.txt", encoding="utf-8", errors="replace").read().strip() + "\n\n./transformations/:\n" + TRANSFORMATIONS + "\n\n" + MEMORY}] + experience + temporary_message
            request_messages, request_tools, transformation_error = apply_transformation(request_messages, TOOLS)
            if transformation_error:
                request_messages += [{"role": "user", "content": transformation_error}]
            request_messages = attach_turn_pdf_images(request_messages)
            if ITER_CONCURRENCY_ENABLED:
                foreground_view = sibling_view(None)  # after the transformations: never logged as user input
                if foreground_view:
                    request_messages = request_messages + [{"role": "user", "content": foreground_view}]
            print("BEFORE LLM")
            _llm_started = time.time()
            try:
                if ITER_CONCURRENCY_ENABLED:  # CONCURRENCY_PORT: promote a call still running after T
                    _foreground_step_guard = _foreground_guard_snapshot(bool(event_append))
                    response, _branch = threaded_llm_call(client, MODEL, request_messages, request_tools, "required", MAX_TOKENS, {"enable_thinking": True})
                else:
                    with _long_step_heartbeat():  # LONG_STEP (2)
                        response = client.chat.completions.create(model=MODEL, messages=request_messages, tools=request_tools, tool_choice="required", max_tokens=MAX_TOKENS, extra_body={ "enable_thinking": True}, extra_headers=route_headers(_route_fg_key))  # HIVE_ROUTE
            except Exception as _llm_error:
                _llm_elapsed = time.time() - _llm_started
                print(f"LLM_CALL_SECONDS {_llm_elapsed:.1f} outcome=error")
                _verdict = route_failure(_route_fg_key, _llm_error, _llm_elapsed, _fg_retries)  # FAILURE_CLIENT
                _last_llm_failure = (_llm_error, _llm_elapsed, _verdict)
                print(f"LLM_FAILURE action={_verdict['action']} class={_verdict.get('class')} "
                      f"local={bool(_verdict.get('local'))} reason={_verdict.get('reason')}")
                if _verdict["action"] == "stop":
                    raise ProviderStop(_verdict.get("reason"), _verdict.get("class")) from _llm_error
                if _verdict["action"] == "agent":
                    _agent_errors += 1
                    if _agent_errors >= AGENT_ERROR_LIMIT:
                        raise ProviderStop(None, None) from _llm_error
                raise
            if response is None:
                print(f"LLM_CALL_SECONDS {time.time() - _llm_started:.1f} outcome=promoted")
                _agent_errors, _long_step_pending = 0, False
                _promoted = True
                break
            print(f"LLM_CALL_SECONDS {time.time() - _llm_started:.1f} outcome=ok")
            route_served(_route_fg_key, response)  # HIVE_ROUTE
            _agent_errors, _long_step_pending = 0, False
            print("AFTER LLM", response)
            message = response.choices[0].message
            _text_only_content = message.content
            if message.content:
                message.content += "\n[NOT DELIVERED TO ANY CHANNEL. IF THIS WAS INTENDED AS COMMUNICATION, USE send.]"
            if message.tool_calls:
                message.tool_calls = message.tool_calls[:MAX_TOOL_CALLS]
                break
            if ITER_CONCURRENCY_ENABLED and response.choices[0].finish_reason != "length":
                text_only_retries += 1  # CONCURRENCY_PORT (B2): bounded text-only re-prompts
                if text_only_retries > TEXT_ONLY_RETRY_CAP:
                    _text_only_idle = True
                    break
            try:
                if response.choices[0].finish_reason == "length":
                    retry_message = [{"role": "user", "content": "[OUTPUT TOKEN LIMIT REACHED. CALL THE REQUIRED TOOL CONCISELY.]"}]
                else:
                    retry_message = [{"role": "user", "content": f"[YOUR PREVIOUS RESPONSE CONTAINED NO TOOL CALL AND WAS NOT DELIVERED. CALL AT LEAST ONE TOOL NOW. IF YOU INTENDED THIS CONTENT AS COMMUNICATION, USE send: {message.content!r}]"}]
            except:
                retry_message = [{"role": "user", "content": f"[YOUR PREVIOUS RESPONSE CONTAINED NO TOOL CALL AND WAS NOT DELIVERED. CALL AT LEAST ONE TOOL NOW. IF YOU INTENDED THIS CONTENT AS COMMUNICATION, USE send: {message.content!r}]"}]
        if _promoted:  # CONCURRENCY_PORT: the branch owns the request; the foreground goes provider-silent
            _route_fg_key = None  # HIVE_ROUTE: the branch continues and ends this request's episode
            new_burst = True
            pending_event_append = ""
            continue
        if _text_only_idle:  # CONCURRENCY_PORT (B2/B7): deliver the text instead of looping
            print(f"TEXT_ONLY_IDLE: retries={text_only_retries}")
            route_episode_end(_route_fg_key, None if answered_this_event else "stalled")  # HIVE_ROUTE
            if not answered_this_event and not send_text_only_fallback(_text_only_content, served_model_tag(response)):
                publish_no_response_notice()
            new_burst, autonomous_steps = True, 0
            pending_event_append = slow_wait_for_input()
            continue
        print(f"RESPONSE {response}\nFINISH_REASON {response.choices[0].finish_reason}\nUSAGE {response.usage}")
        experience += [{key: value for key, value in message.model_dump(exclude_none=True).items() if KEEP_REASONING_IN_EPISODE or key not in ("reasoning", "reasoning_details", "reasoning_content")}]
        tool_outputs = []
        step_results = []
        sent_this_step = False
        model_tag = served_model_tag(response)
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
                        if (tool_name in ("send", "progress") and model_tag and isinstance(tool_arguments.get("content"), str)
                                and tool_arguments["content"] and not tool_arguments["content"].startswith(model_tag)):
                            tool_arguments = dict(tool_arguments, content=model_tag + "\n" + tool_arguments["content"])
                        result = invoke_dynamic(INOPS[tool_name][0], "run", **tool_arguments)
                        ret = result["result"] if result["ok"] else f"Tool execution failed: {result['error']}"
                except Exception as error:
                    ret = f"Tool execution failed: {type(error).__name__}: {error}"
            ret = str(ret)
            tool_tally[tool_name] = tool_tally.get(tool_name, 0) + 1
            if tool_name == "send" and ret == "SUCCESS":
                answered_this_event = True
                sent_this_step = True
                if ITER_CONCURRENCY_ENABLED:
                    _note_send("foreground", tool_arguments.get("content"))
            if tool_name == "progress" and ret == "SUCCESS":
                sent_this_step = True
            if len(ret) > MAX_TOOL_OUTPUT_CHARS:
                ret = ret[:MAX_TOOL_OUTPUT_CHARS] + " [TRUNCATED]"
            experience += [{"role": "tool", "tool_call_id": tool_call.id, "content": "Step " + get_current_time() + ": " + ret}]
            tool_outputs += ["tool call: " + tool_name + " " + str(tool_arguments) + "\n" "tool return: " + ret]
            step_results.append(hashlib.sha256((tool_name + "\x00" + tool_call.function.arguments + "\x00" + ret)
                                               .encode("utf-8", errors="replace")).hexdigest())
        history_checkpoint = len(experience) #tool calls succeeded, even on later exception we won't unroll them
        save_experience(experience)
        print("Output> " + "\n".join(tool_outputs))
        autonomous_steps = 0 if event_append else autonomous_steps + 1
        steps_since_send = 0 if sent_this_step else steps_since_send + 1
        called_nop = any(call.function.name == "nop" for call in message.tool_calls)
        # Runaway backstops (the only conditions that pause for "continue").
        signature = step_signature(message.tool_calls)
        step_signatures = (step_signatures + [signature])[-REPEAT_STEPS:]
        new_results = [digest for digest in step_results if digest not in seen_step_results]
        seen_step_results.update(step_results)
        stale_steps = 0 if (new_results or not step_results) else stale_steps + 1
        request_steps = autonomous_steps + 1  # model steps on this request, counting the event step
        backstop_reason = None
        if not called_nop:
            if request_steps >= HARD_STEP_CEILING:
                backstop_reason = f"I reached my per-request step ceiling ({HARD_STEP_CEILING} steps)"
            elif len(step_signatures) >= REPEAT_STEPS and len(set(step_signatures)) == 1:
                backstop_reason = (f"my last {REPEAT_STEPS} steps repeated the same tool call with the same "
                                   "arguments (possible loop)")
            elif stale_steps >= STALL_STEPS:
                backstop_reason = (f"my last {STALL_STEPS} steps produced no new results "
                                   "(possible loop)")
        if sent_this_step:
            progress_nudge_step = None
        if not called_nop and backstop_reason is None:
            if (request_steps % PROGRESS_EVERY == 0
                    and steps_since_send >= 3 and progress_nudge_step is None):
                # Interim update, not a pause: ask for a short progress note on the
                # next scheduled step (no extra model call) and keep working.
                progress_nudge_step = request_steps
                experience += [{"role": "user", "content": (
                    f"Step {get_current_time()}: [PROGRESS CHECKPOINT: you are at step {request_steps} of this "
                    f"request (runaway ceiling {HARD_STEP_CEILING}) and have not updated the user in the last few "
                    "steps. In your next step, call progress with a concise interim note (done / doing next / "
                    "blockers; no secrets), then KEEP WORKING on the task. Do not stop, do not ask the user to "
                    "type 'continue', and do not use send for this note unless it is your reply or final result. "
                    "Pin key state if useful.]")}]
                save_experience(experience)
            elif (progress_nudge_step is not None
                    and request_steps - progress_nudge_step >= PROGRESS_FALLBACK_STEPS):
                # The model did not post: Iter posts a mechanical tool-tally note
                # itself (no model call, no arguments/outputs) and work continues.
                delivered = publish_progress_note(
                    f"Still working on this (step {request_steps}); not paused, I will keep going. "
                    f"Actions so far: {_tally_text(tool_tally)}.")
                print(f"PROGRESS FALLBACK step={request_steps} delivered={delivered}")
                progress_nudge_step = None
            elif request_steps == HARD_STEP_CEILING - 2 and steps_since_send >= 3:
                experience += [{"role": "user", "content": (
                    f"Step {get_current_time()}: [STEP CEILING WARNING: you are at step {request_steps} of the "
                    f"{HARD_STEP_CEILING}-step runaway ceiling for this request. Within your next step, call send "
                    "with a concise summary (done / remaining / current state; no secrets) and pin the key state "
                    "so you can resume. The user can reply 'continue' to resume.]")}]
                save_experience(experience)
        if called_nop:
            route_episode_end(_route_fg_key, None if answered_this_event else "stalled")  # HIVE_ROUTE
            new_burst, autonomous_steps = True, 0
            if not answered_this_event:
                publish_no_response_notice()
            pending_event_append = slow_wait_for_input()
        elif backstop_reason is not None:
            # Fail closed on a runaway backstop.  Resetting the counter here
            # would allow another unbounded burst immediately.
            new_burst, post_task_mode = True, True
            paused_at_limit = True
            print("STEP BACKSTOP " + backstop_reason)
            route_episode_end(_route_fg_key, "stalled")  # HIVE_ROUTE
            publish_step_limit_notice(request_steps, tool_tally, backstop_reason)
            pending_event_append = slow_wait_for_input()
    except ProviderStop as error:
        print(f"Output> {type(error).__name__}: {error}")
        route_episode_end(_route_fg_key, error.cls or "outage")  # HIVE_ROUTE: a no-op if route_failure ended it
        experience = experience[:history_checkpoint]
        try:
            publish_terminal_failure(provider_stop_text(error.reason))
        except RuntimeError as notice_error:  # was uncaught: a failed notice ended the Iter process
            print(f"PROVIDER FAILURE NOTICE SKIPPED: {notice_error}")
        new_burst, post_task_mode = True, True
        pending_event_append = slow_wait_for_input()
    except Exception as error:
        print(f"Output> {type(error).__name__}: {error}")
        experience = experience[:history_checkpoint]
        _verdict = _last_llm_failure[2] if _last_llm_failure and _last_llm_failure[0] is error else None
        _wait = ERROR_RECOVERY_TIME
        if _verdict and _verdict["action"] == "retry":  # FAILURE_CLIENT: the service's answer
            FailureClient.count(_fg_retries, _verdict)
            _wait = max(_wait, float(_verdict.get("wait_s") or 0))
            if _verdict.get("hint") == "continue_in_parts":  # LONG_STEP (1)
                _long_step_pending = max(1, int(_last_llm_failure[1]))
                print(f"LONG_STEP_RETRY: the step ran {_long_step_pending}s and failed; retrying once with a note to continue in parts")
        elif _verdict and _verdict["action"] == "reroute":
            print("PROVIDER_REROUTE: the routed episode ended; the rest of this request runs on the configured chain")
        time.sleep(_wait)
