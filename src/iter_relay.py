"""Receiver-side relay: bridge authorized Telegram updates to the Iter
outer channel and deliver correlated replies back.

Provider-free and network-free. The live receiver supplies two callables:
  fetch_update() -> (prompt, chat_id, message_id, update_id) | None
  deliver_reply(chat_id, message_id, text) -> bool

One pending update is handled per relay_once() call so the receiver retains
full control of pacing and can interleave its own maintenance work.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from src import iter_outer_channel_protocosmo2 as channel
from src import iter_producer as producer


def relay_once(
    fetch_update: Callable[[], tuple[str, int, int, int] | None],
    deliver_reply: Callable[[int, int, str], bool],
    *,
    outbox_wait_seconds: float = 0.0,
) -> str:
    """Handle at most one update. Returns a short status token.

    Statuses: "idle" (no update), "delivered", "pending" (submitted, reply
    not yet in outbox), "failed-delivery" (outbox entry exists but delivery
    was refused; left for retry on the next pass).
    """
    update = fetch_update()
    if update is None:
        return "idle"
    prompt, chat_id, message_id, update_id = update
    request_id = producer.submit(
        prompt, chat_id=chat_id, message_id=message_id, update_id=update_id)
    result = producer.collect(request_id)
    if result is None:
        return "pending"
    if deliver_reply(chat_id, message_id, result["response"]):
        producer.ack(request_id)
        return "delivered"
    channel.mark_late_pending(request_id)
    return "failed-delivery"


def collect_pending(deliver_reply: Callable[[int, int, str], bool], *,
                    max_age_seconds: float | None = None, now: float | None = None,
                    final_prefix: str = "") -> int:
    """Retry delivery for any previously submitted, not-yet-acked requests.

    Returns the number of replies delivered this pass. The receiver calls
    this each cycle so a restart or transient delivery failure cannot lose
    an already-computed Iter response.

    CONCURRENCY_PORT (PORT 19d0dc3): markers older than max_age_seconds (by
    mtime) are left untouched, so old answers are never posted out of the blue;
    final_prefix is prepended to the late final reply only; a request whose
    progress notes are still queued waits one cycle so notes go out first.
    """
    root = channel._root()
    delivered = 0
    if now is None:
        now = time.time()
    for marker in sorted((root / "late_pending").glob("*.json")):
        if marker.is_symlink():
            continue
        if max_age_seconds is not None:
            try:
                if now - marker.stat().st_mtime > max_age_seconds:
                    continue
            except FileNotFoundError:
                continue
        request_id = marker.stem
        result = producer.collect(request_id)
        if result is None:
            continue
        if any((root / "followups").glob(request_id + ".progress-*.json")):
            continue  # collect_followups delivers the queued progress notes first
        source = result["source"]
        if deliver_reply(source["chat_id"], source["message_id"], final_prefix + result["response"]):
            producer.ack(request_id)
            marker.unlink()
            delivered += 1
    return delivered


def collect_followups(deliver_reply: Callable[[int, int, str], bool]) -> int:
    """Deliver queued follow-ups (oldest first) as replies to their request.

    Stops at the first refused delivery so order is kept; it is retried on
    the next pass. Delivered entries move to followups_sent/.
    """
    root = channel._root()
    delivered = 0
    entries = [path for path in (root / "followups").glob("*.json") if not path.is_symlink()]
    for path in sorted(entries, key=lambda item: (item.stat().st_mtime, item.name)):
        value = json.loads(path.read_text(encoding="utf-8"))
        source = value.get("source")
        if (value.get("identity") != channel._identity() or type(source) is not dict
                or type(source.get("chat_id")) is not int or type(source.get("message_id")) is not int
                or type(value.get("response")) is not str or not value["response"]):
            path.replace(root / "followups_sent" / (path.name + ".rejected"))
            continue
        if not deliver_reply(source["chat_id"], source["message_id"], value["response"]):
            break
        path.replace(root / "followups_sent" / path.name)
        delivered += 1
    return delivered
