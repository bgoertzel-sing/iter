"""Receiver-side producer: convert an authorized Telegram update into an
Iter outer-channel inbox request, and match outbox responses back to the
original source.

This module is provider-free. The live Telegram receiver performs its own
authentication/authorization, then passes (prompt, chat_id, message_id,
update_id) here. The producer writes exactly one request into the channel
inbox; Iter claims exactly one; the outbox response is correlated by the
embedded source envelope. No network or credential handling lives here.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from src import iter_outer_channel_protocosmo2 as channel


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def submit(prompt: str, *, chat_id: int, message_id: int, update_id: int) -> str:
    """Create exactly one inbox request; return its request_id.

    Deterministic request_id per (prompt, source) prevents duplicate
    ingestion when the receiver re-submits after a crash before the cycle
    completes: the filename is the request id, and the same id overwritten
    is idempotent within the receiver's own resume logic.
    """
    if type(chat_id) is not int or type(message_id) is not int or type(update_id) is not int:
        raise channel.ChannelContractError("source identifiers must be exact integers")
    if type(prompt) is not str or not prompt:
        raise channel.ChannelContractError("prompt is invalid")
    root = channel._root()
    source = {"chat_id": chat_id, "message_id": message_id, "update_id": update_id}
    request_id = _digest(json.dumps({"p": prompt, "s": source}, sort_keys=True, separators=(",", ":")))[:64]
    value = {
        "schema_version": channel.SCHEMA_VERSION,
        "identity": channel._identity(),
        "request_id": request_id,
        "prompt": prompt,
        "prompt_sha256": _digest(prompt),
        "source": source,
    }
    path = root / "inbox" / (request_id + ".json")
    path.write_bytes(channel._canonical(value))
    return request_id


def collect(request_id: str, outbox_root_note: str = "") -> dict[str, Any] | None:
    """Return the completed outbox response for a request, if present.

    Reads the outbox entry without mutating Iter-side state. The receiver
    owns extraction: it should move entries from outbox to completed only
    after it has durably delivered the response to Telegram.
    """
    if type(request_id) is not str or len(request_id) != 64:
        raise channel.ChannelContractError("request_id must be 64-hex")
    root = channel._root()
    path = root / "outbox" / (request_id + ".json")
    if not path.is_file() or path.is_symlink():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("request_id") != request_id:
        raise channel.ChannelContractError("outbox correlation mismatch")
    return value


def ack(request_id: str) -> None:
    """Move the outbox response to completed after durable delivery."""
    root = channel._root()
    src = root / "outbox" / (request_id + ".json")
    dst = root / "completed" / (request_id + ".json")
    if not src.is_file():
        raise channel.ChannelContractError("outbox entry missing for ack")
    src.replace(dst)
