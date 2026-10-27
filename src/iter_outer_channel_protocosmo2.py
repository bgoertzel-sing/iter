"""Provider-free Iter channel for the immutable ProtoCosmo2 outer envelope.

The live Telegram receiver remains the sole network and cursor owner.  It puts
one authenticated, already-authorized prompt into this channel; Iter sees only
the prompt and can return only a response bound to the same request metadata.

Multi-request contract (CONCURRENCY_PORT, 2026-10-12): up to
ITER_MAX_OPEN_REQUESTS (default 1, the original single-request contract)
requests may be claimed at once, and send/progress/notify/reminder_source/
close_active accept the request_id they answer.  request_id=None keeps every
original behaviour.  An answered request keeps VM2's follow-up semantics per
request: a later send for it is a follow-up to it (MAX_FOLLOWUPS within
FOLLOWUP_WINDOW_SECONDS), never a second answer.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_PROMPT_CHARS = 200_000
MAX_RESPONSE_CHARS = 200_000
# After a request is answered, send() may add follow-ups to it, delivered
# by the receiver as replies to the same Telegram message. The model gets
# MAX_FOLLOWUPS; notify() may use one more slot for Iter's own notices.
MAX_FOLLOWUPS = 2
MAX_FOLLOWUPS_WITH_NOTICE = 3
FOLLOWUP_WINDOW_SECONDS = 3 * 3600
# Interim progress notes (2026-10-03): progress() posts a reply to the person's
# message WITHOUT answering/closing the request and without spending one of the
# follow-up slots above.  Bounded per request; Telegram caps a message at 4096.
MAX_PROGRESS_NOTES = max(1, int(os.environ.get("ITER_MAX_PROGRESS_NOTES", "12") or "12"))
MAX_PROGRESS_CHARS = 3500
# CONCURRENCY_PORT: notes Iter uses to close a request it can no longer answer.
DEFAULT_UNFINISHED_NOTE = ("(Iter stopped working on this request without sending a final "
                           "answer. Reply to ask for a status update.)")
DEFAULT_TIMEOUT_NOTE = ("(This request timed out before Iter sent a final answer. "
                        "Reply to ask for a status update.)")
MAX_OPEN_REQUESTS_LIMIT = 9


def _identity() -> str:
    # t-2026-10-27-protomega-shared: no default bot name. The launcher must set it.
    identity = os.environ.get("ITER_OUTER_IDENTITY", "")
    if not identity:
        raise ChannelContractError(
            "ITER_OUTER_IDENTITY is required: the bot's launcher must set this bot's identity "
            "(there is no default, so one bot can never take another's identity)")
    if (len(identity) > 128
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in identity)):
        raise ChannelContractError("ITER_OUTER_IDENTITY is invalid")
    return identity


class ChannelContractError(RuntimeError):
    """The filesystem channel state or request violates the frozen contract."""


# Fail at import (t-2026-10-27-protomega-shared): a launcher that forgot
# ITER_OUTER_IDENTITY must not get a working channel under any bot's name.
_identity()


def _canonical(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _root() -> Path:
    raw = os.environ.get("ITER_OUTER_CHANNEL_ROOT")
    if not raw:
        raise ChannelContractError("ITER_OUTER_CHANNEL_ROOT is required")
    root = Path(raw)
    if not root.is_absolute() or root.is_symlink():
        raise ChannelContractError("channel root must be an absolute non-symlink path")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.resolve() != root:
        raise ChannelContractError("channel root must be canonical")
    for name in ("inbox", "processing", "outbox", "completed", "late_pending", "followups", "followups_sent"):
        directory = root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.resolve().parent != root:
            raise ChannelContractError("channel directory escaped its root")
    return root


def mark_late_pending(request_id: str) -> None:
    """Record that the synchronous receiver timed out on this request."""
    if (type(request_id) is not str or len(request_id) != 64
            or any(char not in "0123456789abcdef" for char in request_id)):
        raise ChannelContractError("request_id must be lowercase sha256")
    _atomic_write(_root() / "late_pending" / (request_id + ".json"), {
        "schema_version": SCHEMA_VERSION,
        "identity": _identity(),
        "request_id": request_id,
    })


def _read_exact_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ChannelContractError("request must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"),
                           object_pairs_hook=_reject_duplicate_members)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChannelContractError("request is not canonical JSON") from exc
    if type(value) is not dict:
        raise ChannelContractError("request must be a JSON object")
    return value


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChannelContractError("duplicate JSON member")
        result[key] = value
    return result


def _validate_request(value: dict[str, Any]) -> dict[str, Any]:
    required = {"schema_version", "identity", "request_id", "prompt",
                "prompt_sha256", "source"}
    if set(value) != required:
        raise ChannelContractError("request has an unexpected schema")
    if value["schema_version"] != SCHEMA_VERSION or value["identity"] != _identity():
        raise ChannelContractError("request identity/schema mismatch")
    request_id, prompt, source = value["request_id"], value["prompt"], value["source"]
    if (type(request_id) is not str or len(request_id) != 64
            or any(char not in "0123456789abcdef" for char in request_id)):
        raise ChannelContractError("request_id must be lowercase sha256")
    if type(prompt) is not str or not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise ChannelContractError("prompt is invalid")
    if type(source) is not dict or set(source) != {"chat_id", "message_id", "update_id"}:
        raise ChannelContractError("source envelope is invalid")
    if any(type(source[name]) is not int for name in source):
        raise ChannelContractError("source identifiers must be exact integers")
    if value["prompt_sha256"] != _digest(prompt):
        raise ChannelContractError("prompt digest mismatch")
    return value


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(_canonical(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_create(path: Path, payload: dict[str, Any]) -> None:
    """Write payload to path atomically, failing with FileExistsError if path exists.

    Several Iter threads (tool subprocesses) may write to the channel at once;
    os.link makes "first writer wins" exact, where exists()+replace could race.
    """
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(_canonical(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def max_open_requests() -> int:
    """Configured cap on simultaneously open (claimed) requests.

    ITER_MAX_OPEN_REQUESTS, default 1 (the original single-request contract),
    clamped to 1..MAX_OPEN_REQUESTS_LIMIT so a typo cannot flood the provider.
    """
    raw = os.environ.get("ITER_MAX_OPEN_REQUESTS", "1")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ChannelContractError("ITER_MAX_OPEN_REQUESTS must be an integer") from exc
    return max(1, min(MAX_OPEN_REQUESTS_LIMIT, value))


def _valid_request_id(request_id: Any) -> bool:
    return (type(request_id) is str and len(request_id) == 64
            and all(char in "0123456789abcdef" for char in request_id))


def _open_paths(root: Path) -> list[Path]:
    """Claimed requests, oldest claim first."""
    def key(path: Path) -> tuple[float, str]:
        try:
            return path.stat().st_mtime, path.name
        except FileNotFoundError:
            return float("inf"), path.name
    return sorted((p for p in (root / "processing").glob("*.json")), key=key)


def _load_open(path: Path) -> dict[str, Any]:
    request = _validate_request(_read_exact_json(path))
    if path.name != request["request_id"] + ".json":
        raise ChannelContractError("request filename mismatch")
    return request


def receive_request() -> dict[str, str] | None:
    """Claim the oldest inbox request if a slot is free; return {request_id, prompt}."""
    root = _root()
    active = _open_paths(root)
    if len(active) > max_open_requests():
        raise ChannelContractError("more requests are active than the configured cap")
    if len(active) >= max_open_requests():
        return None
    candidates = sorted((root / "inbox").glob("*.json"))
    if not candidates:
        return None
    request = _validate_request(_read_exact_json(candidates[0]))
    if candidates[0].name != request["request_id"] + ".json":
        raise ChannelContractError("request filename mismatch")
    destination = root / "processing" / candidates[0].name
    os.replace(candidates[0], destination)
    os.utime(destination)  # claim time; expire_stale() measures from here
    return {"request_id": request["request_id"], "prompt": request["prompt"]}


def receive() -> str:
    """Claim at most one request and return its prompt to Iter.

    With the default cap of 1 this is the original contract: nothing is
    claimed while a request is open.
    """
    claimed = receive_request()
    return "" if claimed is None else claimed["prompt"]


def resume() -> str:
    """Return the prompt for exactly one previously claimed request.

    This is deliberately separate from ``receive``: normal polling must not
    redeliver an in-flight request, while a finite restart harness needs an
    explicit, fail-closed recovery operation after process loss.
    """
    root = _root()
    active = sorted((root / "processing").glob("*.json"))
    if len(active) != 1:
        raise ChannelContractError("exactly one active request is required for resume")
    request = _validate_request(_read_exact_json(active[0]))
    if active[0].name != request["request_id"] + ".json":
        raise ChannelContractError("request filename mismatch")
    return request["prompt"]


def resume_all() -> list[dict[str, str]]:
    """Every previously claimed request, oldest claim first."""
    root = _root()
    return [{"request_id": r["request_id"], "prompt": r["prompt"]}
            for r in (_load_open(p) for p in _open_paths(root))]


def active_request_ids() -> list[str]:
    """Ids of all open (claimed, unanswered) requests, oldest claim first."""
    return [p.stem for p in _open_paths(_root())]


def _open_request(root: Path, request_id: str) -> tuple[Path, dict[str, Any]] | None:
    """The open request with this id, or None when it is not (or no longer) open."""
    if not _valid_request_id(request_id):
        raise ChannelContractError("request_id must be lowercase sha256")
    path = root / "processing" / (request_id + ".json")
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path, _load_open(path)
    except ChannelContractError:
        if not path.exists():
            return None  # answered or closed concurrently
        raise


def _answered_request(root: Path, request_id: str) -> tuple[dict[str, int], float] | None:
    """(source, completed_at) of a request that was claimed and is now closed, else None."""
    path = root / "completed" / (request_id + ".json")
    if path.is_symlink() or not path.is_file():
        return None
    value = _read_exact_json(path)  # the request envelope, or the receiver's acked response
    if value.get("request_id") != request_id or value.get("identity") != _identity():
        raise ChannelContractError("answered request identity mismatch")
    source = value.get("source")
    if not _valid_source(source):
        raise ChannelContractError("answered request source is invalid")
    return source, path.stat().st_mtime


def _answer(root: Path, path: Path, request: dict[str, Any], content: str) -> None:
    """Publish the final response for an open request and close it (single winner)."""
    response = {
        "schema_version": SCHEMA_VERSION,
        "identity": _identity(),
        "request_id": request["request_id"],
        "prompt_sha256": request["prompt_sha256"],
        "source": request["source"],
        "response": content,
        "response_sha256": _digest(content),
    }
    output = root / "outbox" / path.name
    try:
        _atomic_create(output, response)
    except FileExistsError:
        raise ChannelContractError("response already exists") from None
    completed = root / "completed" / path.name
    os.replace(path, completed)
    os.utime(completed)  # completion time; the follow-up window measures from here
    _atomic_write(root / "last_completed.json", {
        "schema_version": SCHEMA_VERSION,
        "identity": _identity(),
        "request_id": request["request_id"],
        "source": request["source"],
        "completed_at": int(time.time()),
    })


def send(content: str, request_id: str | None = None) -> None:
    """Publish one Iter response bound to an immutable source.

    request_id=None (original contract): answer the single open request, or,
    when none is open, add a follow-up to the most recently answered one.
    request_id=<sha256>: answer that request if it is open; if it was already
    answered or closed, add a follow-up to it.  Unknown ids are refused.
    """
    if type(content) is not str or not content or len(content) > MAX_RESPONSE_CHARS:
        raise ChannelContractError("response is invalid")
    root = _root()
    if request_id is None:
        active = sorted((root / "processing").glob("*.json"))
        if not active:
            _send_followup(content, MAX_FOLLOWUPS)
            return
        if len(active) != 1:
            raise ChannelContractError(
                "exactly one active request is required" if max_open_requests() == 1
                else "request_id is required when several requests are active")
        _answer(root, active[0], _load_open(active[0]), content)
        return
    found = _open_request(root, request_id)
    if found is None:
        _send_followup(content, MAX_FOLLOWUPS, request_id=request_id)
        return
    _answer(root, found[0], found[1], content)


def _followup_target(root: Path, request_id: str | None, missing: str) -> tuple[str, dict[str, int], float]:
    """(request_id, source, completed_at) a follow-up/progress note attaches to."""
    if request_id is None:
        pointer = root / "last_completed.json"
        if pointer.is_symlink() or not pointer.is_file():
            raise ChannelContractError(missing)
        last = _read_exact_json(pointer)
        if last.get("schema_version") != SCHEMA_VERSION or last.get("identity") != _identity():
            raise ChannelContractError("last answered request identity/schema mismatch")
        rid, source = last.get("request_id"), last.get("source")
        if not _valid_request_id(rid):
            raise ChannelContractError("last answered request id is invalid")
        if not _valid_source(source):
            raise ChannelContractError("last answered request source is invalid")
        return rid, source, float(last.get("completed_at", 0))
    if not _valid_request_id(request_id):
        raise ChannelContractError("request_id must be lowercase sha256")
    answered = _answered_request(root, request_id)
    if answered is None:
        raise ChannelContractError("request is not active (unknown or not yet claimed)")
    return request_id, answered[0], answered[1]


def _send_followup(content: str, allowance: int, request_id: str | None = None) -> None:
    """Queue one follow-up to an answered request (by id, or the most recent one)."""
    root = _root()
    rid, source, completed_at = _followup_target(root, request_id, "no answered request to follow up on")
    if time.time() - completed_at > FOLLOWUP_WINDOW_SECONDS:
        raise ChannelContractError(
            "follow-up window for the last request has expired; wait for the user's next message")
    used = _numbered_followups(root, rid)
    if used >= allowance:
        raise ChannelContractError(
            f"follow-up limit reached for this request ({used} sent); wait for the user's next message")
    for _attempt in range(8):
        index = used + 1
        if index > allowance:
            raise ChannelContractError(
                f"follow-up limit reached for this request ({used} sent); wait for the user's next message")
        try:
            _atomic_create(root / "followups" / f"{rid}.{index}.json", {
                "schema_version": SCHEMA_VERSION,
                "identity": _identity(),
                "request_id": rid,
                "source": source,
                "followup_index": index,
                "response": content,
                "response_sha256": _digest(content),
                "created_at": int(time.time()),
            })
            return
        except FileExistsError:
            used += 1
    raise ChannelContractError("could not allocate a follow-up index")


def _numbered_followups(root: Path, request_id: str) -> int:
    """Count model/notice follow-ups (<id>.<n>.json); progress notes are separate."""
    count = 0
    for directory in ("followups", "followups_sent"):
        for path in (root / directory).glob(request_id + ".*.json"):
            middle = path.name[len(request_id) + 1:-len(".json")]
            if middle.isdigit():
                count += 1
    return count


def _progress_notes(root: Path, request_id: str) -> list[Path]:
    return [path for directory in ("followups", "followups_sent")
            for path in (root / directory).glob(request_id + ".progress-*.json")]


def progress(content: str, request_id: str | None = None) -> None:
    """Queue one interim progress note; the request stays open (or answered).

    Bound to the open request when one is claimed, else to the last answered
    request (within the follow-up window).  With request_id, bound to that
    request: open, or answered within the window.  The receiver delivers it as
    a reply to the person's message on its next cycle.
    """
    if type(content) is not str or not content.strip():
        raise ChannelContractError("progress note is empty")
    if len(content) > MAX_PROGRESS_CHARS:
        raise ChannelContractError(f"progress note too long (max {MAX_PROGRESS_CHARS} chars)")
    root = _root()
    if request_id is None:
        active = sorted((root / "processing").glob("*.json"))
        if len(active) > 1:
            raise ChannelContractError("more than one request is active")
        found = (active[0], _validate_request(_read_exact_json(active[0]))) if active else None
    else:
        found = _open_request(root, request_id)
    if found is not None:
        rid, source = found[1]["request_id"], found[1]["source"]
    else:
        rid, source, completed_at = _followup_target(root, request_id, "no request to post progress on")
        if time.time() - completed_at > FOLLOWUP_WINDOW_SECONDS:
            raise ChannelContractError("progress window for the last request has expired")
    used = len(_progress_notes(root, rid))
    for _attempt in range(8):
        if used >= MAX_PROGRESS_NOTES:
            raise ChannelContractError(
                f"progress-note limit reached for this request ({used}); keep working and "
                "report progress in your reply or final result")
        index = used + 1
        try:
            _atomic_create(root / "followups" / f"{rid}.progress-{index:03d}.json", {
                "schema_version": SCHEMA_VERSION,
                "identity": _identity(),
                "request_id": rid,
                "source": source,
                "kind": "progress",
                "progress_index": index,
                "response": content,
                "response_sha256": _digest(content),
                "created_at": int(time.time()),
            })
            return
        except FileExistsError:
            used += 1
    raise ChannelContractError("could not allocate a progress index")


def _valid_source(source: Any) -> bool:
    return (type(source) is dict and set(source) == {"chat_id", "message_id", "update_id"}
            and all(type(source[name]) is int for name in source))


def reminder_source(request_id: str | None = None) -> str:
    """JSON source that a reminder set now should reply to when it fires.

    That is the open request, else the last answered one (with request_id:
    that request, open or answered). Returned as JSON text because Iter's
    dynamic worker stringifies return values.
    """
    root = _root()
    if request_id is not None:
        found = _open_request(root, request_id)
        if found is not None:
            return json.dumps(found[1]["source"])
        answered = _answered_request(root, request_id)
        if answered is not None:
            return json.dumps(answered[0])
        raise ChannelContractError("no request to attach the reminder to")
    active = sorted((root / "processing").glob("*.json"))
    if len(active) == 1:
        return json.dumps(_validate_request(_read_exact_json(active[0]))["source"])
    pointer = root / "last_completed.json"
    if pointer.is_file() and not pointer.is_symlink():
        last = _read_exact_json(pointer)
        if last.get("identity") == _identity() and _valid_source(last.get("source")):
            return json.dumps(last["source"])
    raise ChannelContractError("no request to attach the reminder to")


def deliver_reminder(content: str, source: dict[str, int], alarm_id: str) -> None:
    """Queue a due reminder as a reply to its source (delivered by the receiver)."""
    if type(content) is not str or not content or len(content) > MAX_RESPONSE_CHARS:
        raise ChannelContractError("reminder text is invalid")
    if not _valid_source(source):
        raise ChannelContractError("reminder source is invalid")
    if (type(alarm_id) is not str or not alarm_id or len(alarm_id) > 64
            or any(char not in "0123456789abcdefghijklmnopqrstuvwxyz-" for char in alarm_id)):
        raise ChannelContractError("alarm id is invalid")
    _atomic_write(_root() / "followups" / f"reminder-{alarm_id}.json", {
        "schema_version": SCHEMA_VERSION,
        "identity": _identity(),
        "request_id": "reminder",
        "source": source,
        "response": content,
        "response_sha256": _digest(content),
        "created_at": int(time.time()),
    })


def notify(content: str, request_id: str | None = None) -> None:
    """Iter's own notices: answer the open request, or use the reserved follow-up slot."""
    if type(content) is not str or not content or len(content) > MAX_RESPONSE_CHARS:
        raise ChannelContractError("response is invalid")
    root = _root()
    if request_id is None:
        if sorted((root / "processing").glob("*.json")):
            send(content)
            return
        _send_followup(content, MAX_FOLLOWUPS_WITH_NOTICE)
        return
    found = _open_request(root, request_id)
    if found is not None:
        _answer(root, found[0], found[1], content)
        return
    _send_followup(content, MAX_FOLLOWUPS_WITH_NOTICE, request_id=request_id)


def close_active(note: str = DEFAULT_UNFINISHED_NOTE, request_id: str | None = None) -> bool:
    """Close a request that is still open with a short note; True if this call closed it.

    Never a follow-up: an already answered or closed request is left alone.
    request_id=None closes the single open request (refused when several are open).
    """
    if type(note) is not str or not note or len(note) > MAX_RESPONSE_CHARS:
        raise ChannelContractError("note is invalid")
    root = _root()
    if request_id is None:
        active = _open_paths(root)
        if not active:
            return False
        if len(active) > 1:
            raise ChannelContractError("request_id is required when several requests are active")
        request_id = active[0].stem
    found = _open_request(root, request_id)
    if found is None:
        return False
    try:
        _answer(root, found[0], found[1], note)
    except FileNotFoundError:
        return False  # moved by a concurrent answer
    except ChannelContractError:
        # Another writer won: its outbox entry exists (it may not have moved the
        # request out of processing/ yet), or the request is no longer open.
        if (root / "outbox" / (request_id + ".json")).exists() or _open_request(root, request_id) is None:
            return False
        raise
    return True


def _last_activity(root: Path, path: Path) -> float:
    """Claim time, or the newest progress note for the request if later."""
    latest = path.stat().st_mtime
    for note in _progress_notes(root, path.stem):
        try:
            latest = max(latest, note.stat().st_mtime)
        except FileNotFoundError:
            continue  # delivered concurrently (moved to followups_sent/)
    return latest


def expire_stale(max_age_seconds: float, note: str = DEFAULT_TIMEOUT_NOTE,
                 now: float | None = None, keep: list[str] | None = None) -> bool:
    """Close every open request with no claim/progress activity for max_age_seconds.

    A long task that keeps posting progress notes stays open; the clock runs
    from its newest note.  Ids in keep (requests a live Iter thread still owns)
    are never expired here: their owner answers or closes them.  Returns True
    if any request was closed.
    """
    if type(max_age_seconds) not in (int, float) or max_age_seconds <= 0:
        raise ChannelContractError("max_age_seconds must be positive")
    current = time.time() if now is None else now
    keep_ids = set(keep or ())
    closed = False
    root = _root()
    for path in _open_paths(root):
        if path.stem in keep_ids:
            continue
        try:
            if current - _last_activity(root, path) < max_age_seconds:
                continue
        except FileNotFoundError:
            continue  # answered concurrently
        if close_active(note, request_id=path.stem):
            closed = True
    return closed
