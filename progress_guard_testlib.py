"""Test helper (PROGRESS_NOT_PAUSE, 2026-10-04): put iter.py's StepGuard into an exec namespace.

iter.py runs its main loop at import time, so tests exec function segments into a
dict namespace.  Since the progress-not-pause port, _bg_branch_mini_loop uses
StepGuard; add_step_guard(ns) supplies it with the default policy values.
"""
import ast
import copy
import hashlib
import json
import time
from pathlib import Path

_SRC = (Path(__file__).resolve().parent / "iter.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)
NAMES = ("step_signature", "_tally_text", "StepGuard")
DEFAULTS = {"PROGRESS_EVERY": 30, "PROGRESS_FALLBACK_STEPS": 2, "PROGRESS_MAX_SILENT_SECONDS": 420,
            "HARD_STEP_CEILING": 300, "REPEAT_STEPS": 8, "STALL_STEPS": 12}


def add_step_guard(ns, **overrides):
    for name, value in {"copy": copy, "hashlib": hashlib, "json": json, "time": time}.items():
        ns.setdefault(name, value)
    for name, value in DEFAULTS.items():
        ns.setdefault(name, value)
    ns.update(overrides)
    ns.setdefault("send_progress_note", lambda content, tag="": True)
    for name in NAMES:
        node = next(n for n in _TREE.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
        exec(ast.get_source_segment(_SRC, node), ns)
    return ns
