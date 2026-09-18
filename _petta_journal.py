"""Shared PeTTa-memory journal plumbing for Iter transformations/tools.

Design (per approved v2 plan):
- History append-only: the journal file on disk is only ever appended to; the
  supersession-resolved view is computed in-memory per read. No rewrite ever.
- All functions are read/read or read/append and never raise.
- Supersedes resolution filters stale facts from every rendered view.
"""
import os, re, threading, datetime
from pathlib import Path

_LOCK = threading.Lock()

# Hoisted compiled patterns (perf: avoids per-call regex dispatch; 09-03)
_RE_SUPERSEDES = re.compile(r"\(Supersedes\s+(\S+)\s+(\S+)\)")
_RE_MEMORY_CLUSTER = re.compile(r"\(MemoryCluster\s+([^\s)]+)")

def _journal_dir() -> Path:
    """Return the journal directory (from JOURNAL_DIR env var or default location)."""
    env = os.environ.get("JOURNAL_DIR")
    base = Path(env) if env else Path(__file__).resolve().parent / "memory" / "_journal"
    base.mkdir(parents=True, exist_ok=True)
    return base

def _journal_path() -> Path:
    """Return the full path to the journal metta file."""
    return _journal_dir() / "journal.metta"

def _read_lines() -> list[str]:
    """Read all journal lines from disk, returning an empty list on any error."""
    try:
        p = _journal_path()
        if not p.exists():
            return []
        return p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

def _collect_superseded_ids(lines: list[str]) -> set[str]:
    """Scan all lines for (Supersedes new-id old-id) pairs and return the set of superseded old-ids."""
    superseded = set()
    for line in lines:
        if "(Supersedes" not in line:
            continue
        for m in _RE_SUPERSEDES.finditer(line):
            new_id, old_id = m.group(1), m.group(2)
            if new_id != "new-id" and old_id != "old-id":
                superseded.add(old_id)
    return superseded

def _filter_superseded(lines: list[str], superseded: set[str]) -> list[str]:
    """Walk lines, dropping clusters whose BEGIN id is in the superseded set."""
    out = []
    dropping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(";;; BEGIN MemoryCluster"):
            dropping = stripped.rsplit(" ", 1)[-1] in superseded
            if not dropping:
                out.append(line)
        elif stripped.startswith(";;; END MemoryCluster"):
            if not dropping:
                out.append(line)
            dropping = False
        elif not dropping:
            out.append(line)
    return out

def resolve_supersedes(lines: list[str]) -> list[str]:
    """Given journal lines, return lines with superseded clusters removed."""
    try:
        superseded = _collect_superseded_ids(lines)
        return _filter_superseded(lines, superseded)
    except Exception:
        return lines

def recall(limit: int = 20) -> dict:
    """Return the supersession-resolved journal view (read-only)."""
    lines = _read_lines()
    resolved = resolve_supersedes(lines)
    return {"ok": True, "count": len(resolved), "view": resolved[-limit:] if limit else resolved}

def append_note(note: str) -> dict:
    """Append a note as a structured Episode cluster (append-only write).

    Structured (not loose) so MediumMemoryStore.append_cluster keeps working
    on the journal; loose (Episode (note)) lines break structured append.
    """
    try:
        note = str(note)
        safe = note.replace("\\", "\\\\").replace('"', '\\"')
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cid = "ep-note-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ev = "ev-" + cid
        blk = (
            f";;; BEGIN MemoryCluster {cid}\n"
            f"(MemoryCluster {cid})\n"
            f"(SchemaVersion {cid} medium-memory-v1)\n"
            f"(ClusterType {cid} Episode)\n"
            f"(ClusterSource {cid} iter-experience)\n"
            f"(ClusterOpenedAt {cid} {ts})\n"
            f"(Contains {cid} {ev})\n"
            f"(ObservedEvent {ev})\n"
            f"(About {cid} journal)\n"
            f'(EventNote {ev} "{safe}")\n'
            f";;; END MemoryCluster {cid}\n"
        )
        with _LOCK:
            p = _journal_path()
            with open(p, "a", encoding="utf-8") as f:
                f.write(blk)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def current_beliefs() -> dict:
    """Return resolved view containing only current (non-superseded) lines."""
    lines = _read_lines()
    resolved = resolve_supersedes(lines)
    return {"ok": True, "resolved": resolved, "count": len(resolved)}
