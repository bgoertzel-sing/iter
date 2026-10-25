"""Authenticated filesystem channel owned exclusively by ProtoMegaBot2."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Every hook is imported directly (never in a guarded try/except): a shim that
# names a hook its contract lacks must fail loudly at import (Sep 30 B5 lesson).
from src.iter_outer_channel_protocosmo2 import (  # noqa: E402,F401
    active_request_ids, close_active, deliver_reminder, expire_stale, notify, progress,
    receive, receive_request, reminder_source, resume, resume_all, send)
