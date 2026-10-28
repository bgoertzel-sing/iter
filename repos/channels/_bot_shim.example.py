"""Example per-bot channel shim (not loaded: Iter skips channels/_*.py).

Each bot keeps its own untracked copy as channels/<channel>.py (for example
channels/protomega2.py), owned exclusively by <BOT_NAME>.  The bot's launcher
must set ITER_OUTER_IDENTITY=<BOT_NAME> before Iter starts; the transport
refuses to import without it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Every hook is imported directly (never in a guarded try/except): a shim that
# names a hook its contract lacks must fail loudly at import (Sep 30 B5 lesson).
import src.iter_outer_channel_protocosmo2 as _impl  # noqa: E402
from src.iter_outer_channel_protocosmo2 import (  # noqa: E402,F401
    active_request_ids, close_active, deliver_reminder, expire_stale, notify, progress,
    reminder_source, send)

# hive task t-2026-10-24-telegram-message-cite: the request file's source
# envelope ({chat_id, message_id, update_id}, validated as exact ints by the
# contract) was dropped before the prompt reached the model, so proposals
# could not cite the Telegram message.  Prefix every prompt with it.


def cite(source: dict[str, Any]) -> str:
    """telegram:<chat_id>/<message_id> for a validated source envelope."""
    if not _impl._valid_source(source):
        raise _impl.ChannelContractError("source envelope is invalid")
    return f"telegram:{source['chat_id']}/{source['message_id']}"


def _cited(prompt: str, source: dict[str, Any]) -> str:
    return f"(cite as {cite(source)}) {prompt}"


def _load(path: Path) -> dict[str, Any]:
    return _impl._load_open(path)


def receive_request() -> dict[str, str] | None:
    claimed = _impl.receive_request()
    if claimed is None:
        return None
    request = _load(_impl._root() / "processing" / (claimed["request_id"] + ".json"))
    return {"request_id": claimed["request_id"],
            "prompt": _cited(claimed["prompt"], request["source"])}


def receive() -> str:
    claimed = receive_request()
    return "" if claimed is None else claimed["prompt"]


def resume() -> str:
    _impl.resume()  # enforces the exactly-one-active-request contract
    (path,) = sorted((_impl._root() / "processing").glob("*.json"))
    request = _load(path)
    return _cited(request["prompt"], request["source"])


def resume_all() -> list[dict[str, str]]:
    return [{"request_id": r["request_id"], "prompt": _cited(r["prompt"], r["source"])}
            for r in (_load(p) for p in _impl._open_paths(_impl._root()))]
