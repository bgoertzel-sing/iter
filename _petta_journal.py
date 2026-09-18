"""Shared PeTTa-memory journal plumbing for Iter transformations/tools.

Design (per approved v2 plan):
- History append-only: the journal file on disk is only ever appended to; the
  supersession-resolved view is computed in-memory per read. No rewrite ever.
- All functions are read/read or read/append and never raise.
- Supersedes resolution filters stale facts from every rendered view.
- Quote-aware: Supersedes directives inside quoted EventNote content are ignored.
- Collision-resistant IDs: timestamps include microsecond precision + counter.
- Structured cluster append: append_cluster() for derived belief round-tripping.
"""
import os, re, threading, datetime, itertools
from pathlib import Path

_LOCK = threading.Lock()
_ID_COUNTER = itertools.count()

# Hoisted compiled patterns (perf: avoids per-call regex dispatch; 09-03)
_RE_SUPERSEDES = re.compile(r"\(Supersedes\s+(\S+)\s+(\S+)\)")
_RE_MEMORY_CLUSTER = re.compile(r"\(MemoryCluster\s+([^\s)]+)")
_RE_BEGIN = re.compile(r";;;\s*BEGIN\s+MemoryCluster\s+(\S+)")
_RE_END = re.compile(r";;;\s*END\s+MemoryCluster\s+(\S+)")
_RE_EVENT_NOTE = re.compile(r'\(EventNote\s+\S+\s+"(.*)"\s*\)')

def _journal_dir() -> Path:
    """Return the journal directory (from JOURNAL_DIR env var or default location)."""
    env = os.environ.get("JOURNAL_DIR")
    base = Path(env) if env else Path(__file__).resolve().parent / "memory" / "_journal"
    base.mkdir(parents=True, exist_ok=True)
    return base

def _journal_path() -> Path:
    """Return the full path to the journal metta file."""
    return _journal_dir() / "journal.metta"

def _make_id(prefix: str = "ep-note") -> str:
    """Generate a collision-resistant ID using microsecond precision + counter."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    counter = next(_ID_COUNTER)
    return f"{prefix}-{ts}-{counter:04d}"

def _read_lines() -> list[str]:
    """Read all journal lines from disk, returning an empty list on any error."""
    try:
        p = _journal_path()
        if not p.exists():
            return []
        return p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

def _is_quoted_line(line: str) -> bool:
    """Check if a line contains a quoted EventNote (control statements inside quotes should be ignored)."""
    return bool(_RE_EVENT_NOTE.search(line))

def _extract_supersedes_from_control(line: str) -> list[tuple[str, str]]:
    """Extract (new_id, old_id) pairs from a Supersedes control line.

    Only matches on unquoted control lines, NOT inside EventNote content.
    """
    if _is_quoted_line(line):
        # This is a quoted EventNote - extract the note content and check
        # if Supersedes appears inside the quotes. If so, skip it.
        m = _RE_EVENT_NOTE.search(line)
        if m:
            note_content = m.group(1)
            # If the entire line is a quoted EventNote with Supersedes inside, skip
            if "(Supersedes" in note_content:
                return []
        # Even if not a clean match, be conservative
        return []
    pairs = []
    for m in _RE_SUPERSEDES.finditer(line):
        new_id, old_id = m.group(1), m.group(2)
        if new_id != "new-id" and old_id != "old-id":
            pairs.append((new_id, old_id))
    return pairs

def _collect_superseded_ids(lines: list[str]) -> set[str]:
    """Scan all lines for (Supersedes new-id old-id) pairs and return the set of superseded old-ids.

    Quote-aware: Supersedes text inside quoted EventNote content is ignored.
    """
    superseded = set()
    for line in lines:
        if "(Supersedes" not in line:
            continue
        for new_id, old_id in _extract_supersedes_from_control(line):
            superseded.add(old_id)
    return superseded

def _filter_superseded(lines: list[str], superseded: set[str]) -> list[str]:
    """Walk lines, dropping clusters whose BEGIN id is in the superseded set.

    Also validates that END id matches BEGIN id; mismatched END markers
    do not resurrect superseded clusters.
    """
    out = []
    dropping = False
    current_id = None
    for line in lines:
        stripped = line.strip()
        begin_match = _RE_BEGIN.match(stripped)
        if begin_match:
            current_id = begin_match.group(1)
            dropping = current_id in superseded
            if not dropping:
                out.append(line)
        elif _RE_END.match(stripped):
            end_match = _RE_END.match(stripped)
            end_id = end_match.group(1) if end_match else None
            if not dropping and (end_id is None or end_id == current_id):
                out.append(line)
            elif not dropping and end_id != current_id:
                # Mismatched END id - still close the cluster but don't resurrect
                out.append(line)
            dropping = False
            current_id = None
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
        cid = _make_id("ep-note")
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

def append_cluster(cluster_id: str, cluster_type: str, lines: list[str],
                   source: str = "wmtm-writeback") -> dict:
    """Append a structured MemoryCluster with arbitrary body lines.

    This provides a proper round-trippable interface for derived beliefs,
    avoiding the EventNote escaping mismatch in append_note().

    Args:
        cluster_id: Unique cluster identifier (collision-resistant)
        cluster_type: e.g. 'DerivedBelief', 'Episode'
        lines: List of MeTTa content lines for the cluster body
        source: Source identifier

    Returns:
        {"ok": True} on success, {"ok": False, "error": ...} on failure
    """
    try:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cid = cluster_id or _make_id("wmtm-cluster")
        header = [
            f";;; BEGIN MemoryCluster {cid}",
            f"(MemoryCluster {cid})",
            f"(SchemaVersion {cid} medium-memory-v1)",
            f"(ClusterType {cid} {cluster_type})",
            f"(ClusterSource {cid} {source})",
            f"(ClusterOpenedAt {cid} {ts})",
        ]
        footer = [f";;; END MemoryCluster {cid}"]
        body_lines = header + lines + footer
        blk = "\n".join(body_lines) + "\n"
        with _LOCK:
            p = _journal_path()
            with open(p, "a", encoding="utf-8") as f:
                f.write(blk)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def append_derived_cluster(
    cluster_id: str,
    note: str,
    derived_from: list | None = None,
    inference_type: str = "",
    confidence: float = 0.0,
) -> dict:
    """Append a DerivedBelief cluster (append-only write).

    Uses a single-line format with ClusterType DerivedBelief that
    the recall parser can round-trip. Returns {'ok': True} on success.
    """
    try:
        safe = str(note).replace("\\", "\\\\").replace('"', '\\"')
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cid = cluster_id
        ev = "ev-" + cid
        lines = [
            f";;; BEGIN MemoryCluster {cid}",
            f"(MemoryCluster {cid})",
            f"(SchemaVersion {cid} medium-memory-v1)",
            f"(ClusterType {cid} DerivedBelief)",
            f"(ClusterSource {cid} wmtm-writeback)",
            f"(ClusterOpenedAt {cid} {ts})",
            f"(Contains {cid} {ev})",
            f"(ObservedEvent {ev})",
            f"(About {cid} wmtm-derived)",
            f'(EventNote {ev} "{safe}")',
        ]
        for src in (derived_from or []):
            lines.append(f"(DerivedFrom {cid} {src})")
        if inference_type:
            lines.append(f"(InferenceType {cid} {inference_type})")
        if confidence > 0:
            lines.append(f"(InferenceConfidence {cid} {confidence:.4f})")
        lines.append(f";;; END MemoryCluster {cid}")
        blk = "\n".join(lines) + "\n"
        with _LOCK:
            p = _journal_path()
            with open(p, "a", encoding="utf-8") as f:
                f.write(blk)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def append_cluster(cluster_body: str) -> dict:
    """Append a pre-formatted MemoryCluster block directly to the journal.

    Unlike append_note (which wraps content in an escaped EventNote),
    this writes the cluster body as-is, preserving structure for
    round-trip parsing by the recall bridge.

    The cluster_body should contain valid metta with BEGIN/END markers.
    Returns {"ok": True} on success, {"ok": False, "error": ...} on failure.
    """
    try:
        body = str(cluster_body).strip()
        if not body:
            return {"ok": False, "error": "empty cluster body"}
        if ";;; BEGIN MemoryCluster" not in body:
            return {"ok": False, "error": "missing BEGIN marker"}
        if ";;; END MemoryCluster" not in body:
            return {"ok": False, "error": "missing END marker"}
        with _LOCK:
            p = _journal_path()
            with open(p, "a", encoding="utf-8") as f:
                f.write(body)
                if not body.endswith("\n"):
                    f.write("\n")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def current_beliefs() -> dict:
    """Return resolved view containing only current (non-superseded) lines."""
    lines = _read_lines()
    resolved = resolve_supersedes(lines)
    return {"ok": True, "resolved": resolved, "count": len(resolved)}
