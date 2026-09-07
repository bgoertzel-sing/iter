"""Iter channel for ProtoCosmo2's authenticated outer transport.

The contract implementation lives outside the pinned Iter checkout so the
transport/state boundary can be versioned and tested independently.  Resolve
it relative to this checkout; no live path or credential is embedded here.
"""
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.iter_outer_channel_protocosmo2 import receive, resume, send  # noqa: E402,F401
