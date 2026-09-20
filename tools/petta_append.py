"""Append a note/Episode entry to the PeTTa journal (append-only)."""
import importlib.util
from pathlib import Path

DESCRIPTION = "Append a note to the PeTTa journal (append-only write). Arg: note (string)."

def _journal():
    """Import and return the _petta_journal module."""
    p = Path(__file__).resolve().parent.parent / "_petta_journal.py"
    spec = importlib.util.spec_from_file_location("_petta_journal", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def run(note):
    """Append *note* as a structured episode to the PeTTa journal."""
    j = _journal()
    r = j.append_note(note)
    return "SUCCESS" if r.get("ok") else f"FAIL: {r.get('error')}"
