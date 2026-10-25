"""Iter-backed responder: drop-in replacement for the receiver's
``responder(text)`` callable.

For each authorized inbound text, the responder submits an Iter outer-channel
request, waits for Iter's correlated outbox response, and returns the reply
text so the receiver can deliver it through its normal path. The synchronous
blocking call keeps the existing receiver flow unchanged (fetch → respond →
deliver) while Iter runs as the model engine behind the channel.

Timeout behavior: if Iter has not answered within ``timeout_seconds``, a
fail-closed error is raised; the request stays in the channel so the reply
is not lost and can still be delivered via relay.collect_pending() after
the receiver retries or restarts.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from src import iter_outer_channel_protocosmo2 as channel
from src import iter_producer as producer


# A request that is still open but posting interim progress notes (Iter
# progress(), 2026-10-03) is alive: each fresh note extends the wait by up to
# timeout_seconds, never beyond ITER_RESPONDER_MAX_SECONDS from submission.
RESPONDER_MAX_SECONDS = float(os.environ.get("ITER_RESPONDER_MAX_SECONDS", "10800") or "10800")


def _latest_progress_age(request_id: str) -> float | None:
    try:
        root = channel._root()
    except Exception:
        return None
    newest = None
    for directory in ("followups", "followups_sent"):
        for path in (root / directory).glob(request_id + ".progress-*.json"):
            try:
                mtime = path.lstat().st_mtime
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
    return None if newest is None else max(0.0, time.time() - newest)


def make_responder(
    *,
    chat_id: int,
    timeout_seconds: float = 240.0,
    poll_interval: float = 0.5,
) -> Any:
    """Return a responder(text) callable bound to a fixed chat.

    The receiver's message handler knows the chat/message/update ids at call
    time; ``make_responder`` captures the stable chat identity and produces a
    function with the exact ``responder(text) -> str`` signature the runner
    already uses. Per-message ids are passed through ``bind``.
    """
    # Thread-local (CONCURRENCY_PORT, PORT 88958e5): the receiver's promotable and
    # deferred workers call bind() then respond() for different messages of the
    # same chat concurrently; a shared dict could attach a reply to the wrong one.
    local = threading.local()

    def _bound() -> dict[str, int]:
        value = getattr(local, "ids", None)
        if value is None:
            value = local.ids = {"message_id": 0, "update_id": 0}
        return value

    def bind(message_id: int, update_id: int) -> None:
        if type(message_id) is not int or type(update_id) is not int:
            raise channel.ChannelContractError("message/update ids must be integers")
        bound = _bound()
        bound["message_id"] = message_id
        bound["update_id"] = update_id

    def respond(text: str) -> str:
        bound = _bound()
        request_id = producer.submit(
            text,
            chat_id=chat_id,
            message_id=bound["message_id"],
            update_id=bound["update_id"],
        )
        started = time.monotonic()
        deadline = started + timeout_seconds
        hard_deadline = started + max(timeout_seconds, RESPONDER_MAX_SECONDS)
        while True:
            result = producer.collect(request_id)
            if result is not None:
                return str(result["response"])
            now = time.monotonic()
            if now >= deadline:
                age = _latest_progress_age(request_id)
                if age is None or age >= timeout_seconds or now >= hard_deadline:
                    break
                deadline = min(hard_deadline, now + (timeout_seconds - age))
                continue
            time.sleep(poll_interval)
        channel.mark_late_pending(request_id)
        raise TimeoutError(f"Iter did not answer request {request_id} within {timeout_seconds}s")

    respond.bind = bind  # type: ignore[attr-defined]
    return respond
