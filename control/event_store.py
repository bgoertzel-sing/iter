"""Transactional event store for goal-driven iter control.

SQLite-backed append-only event log with crash recovery, replay, and
reconciliation. Events are immutable; corrections append superseding events.

Safety invariants:
- I3: Effect-bearing requests carry a revision matching the current snapshot.
- I7: Persisted events are immutable and replayable from a frozen log.
- I8: Every dispatched action is reconciled against the event log.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from control.types import EventRecord, now_iso


class EventStore:
    """Append-only transactional event store backed by SQLite.

    All writes are atomic. Reads return immutable EventRecord objects.
    The store supports crash recovery via WAL mode and replay.
    """

    def __init__(self, db_path: str, allow_init: bool = True):
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        if allow_init:
            self._init_schema()

    def _init_schema(self):
        """Create tables if they do not exist."""
        with self._lock:
            self._conn.executescript(
                """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                revision INTEGER NOT NULL,
                payload TEXT NOT NULL,
                prev_event_id TEXT,
                superseded INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                revision INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                snapshot_data TEXT NOT NULL,
                event_id TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_revision ON events(revision);
            CREATE INDEX IF NOT EXISTS idx_events_prev ON events(prev_event_id);
            """
            )

    def append(self, event: EventRecord) -> str:
        """Append an event atomically. Returns the event_id.

        Raises ValueError if event_id already exists (idempotency guard).
        """
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO events (event_id, event_type, timestamp, revision, payload, prev_event_id, superseded) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (
                        event.event_id,
                        event.event_type,
                        event.timestamp,
                        event.revision,
                        event.payload,
                        event.prev_event_id,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Event {event.event_id} already exists")
            return event.event_id

    def get(self, event_id: str) -> Optional[EventRecord]:
        """Retrieve a single event by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT event_id, event_type, timestamp, revision, payload, prev_event_id "
                "FROM events WHERE event_id = ? AND superseded = 0",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return EventRecord(
            event_id=row[0],
            event_type=row[1],
            timestamp=row[2],
            revision=row[3],
            payload=row[4],
            prev_event_id=row[5],
        )

    def get_all(self) -> list[EventRecord]:
        """Retrieve all non-superseded events in order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, event_type, timestamp, revision, payload, prev_event_id "
                "FROM events WHERE superseded = 0 ORDER BY revision, timestamp"
            ).fetchall()
        return [
            EventRecord(
                event_id=r[0], event_type=r[1], timestamp=r[2],
                revision=r[3], payload=r[4], prev_event_id=r[5],
            )
            for r in rows
        ]

    def get_by_type(self, event_type: str) -> list[EventRecord]:
        """Retrieve all non-superseded events of a given type."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, event_type, timestamp, revision, payload, prev_event_id "
                "FROM events WHERE event_type = ? AND superseded = 0 ORDER BY revision, timestamp",
                (event_type,),
            ).fetchall()
        return [
            EventRecord(
                event_id=r[0], event_type=r[1], timestamp=r[2],
                revision=r[3], payload=r[4], prev_event_id=r[5],
            )
            for r in rows
        ]

    def supersede(self, old_event_id: str, new_event: EventRecord) -> str:
        """Mark old_event_id as superseded and append new_event.

        This is atomic: both operations succeed or neither does.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "UPDATE events SET superseded = 1 WHERE event_id = ?",
                    (old_event_id,),
                )
                self._conn.execute(
                    "INSERT INTO events (event_id, event_type, timestamp, revision, payload, prev_event_id, superseded) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (
                        new_event.event_id,
                        new_event.event_type,
                        new_event.timestamp,
                        new_event.revision,
                        new_event.payload,
                        old_event_id,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return new_event.event_id

    def current_revision(self) -> int:
        """Return the highest revision number in the store."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(revision) FROM events WHERE superseded = 0"
            ).fetchone()
        return row[0] if row[0] is not None else 0

    def event_count(self) -> int:
        """Return the total number of non-superseded events."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE superseded = 0"
            ).fetchone()
        return row[0] if row[0] is not None else 0

    def save_snapshot(self, revision: int, snapshot_data: dict, event_id: str):
        """Save a state snapshot at a given revision."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO snapshots (revision, timestamp, snapshot_data, event_id) "
                "VALUES (?, ?, ?, ?)",
                (revision, now_iso(), json.dumps(snapshot_data, sort_keys=True), event_id),
            )

    def load_snapshot(self, revision: int) -> Optional[dict]:
        """Load a state snapshot at a given revision."""
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_data FROM snapshots WHERE revision = ?", (revision,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def latest_snapshot(self) -> Optional[dict]:
        """Load the most recent snapshot."""
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_data FROM snapshots ORDER BY revision DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def replay_from(self, revision: int) -> list[EventRecord]:
        """Replay all events from a given revision onward."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, event_type, timestamp, revision, payload, prev_event_id "
                "FROM events WHERE revision >= ? AND superseded = 0 ORDER BY revision, timestamp",
                (revision,),
            ).fetchall()
        return [
            EventRecord(
                event_id=r[0], event_type=r[1], timestamp=r[2],
                revision=r[3], payload=r[4], prev_event_id=r[5],
            )
            for r in rows
        ]

    def reconcile(self) -> dict:
        """Reconcile the event log: check for gaps, duplicates, and orphans.

        Returns a report dict with keys: complete, gaps, duplicates,
        orphaned, event_count, revision_range.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT revision, event_id, prev_event_id FROM events "
                "WHERE superseded = 0 ORDER BY revision"
            ).fetchall()

        revisions = [r[0] for r in rows]
        gaps = []
        if revisions:
            for i in range(1, max(revisions) + 1):
                if i not in revisions:
                    gaps.append(i)

        rev_counts: dict[int, int] = {}
        for r in rows:
            rev_counts[r[0]] = rev_counts.get(r[0], 0) + 1
        duplicates = [k for k, v in rev_counts.items() if v > 1]

        # Check for orphaned supersede references
        all_ids = {r[1] for r in rows}
        orphaned = []
        for r in rows:
            if r[2] and r[2] not in all_ids:
                orphaned.append(r[2])

        return {
            "complete": len(gaps) == 0 and len(duplicates) == 0,
            "gaps": gaps,
            "duplicates": duplicates,
            "orphaned": orphaned,
            "event_count": len(rows),
            "revision_range": (min(revisions), max(revisions)) if revisions else (0, 0),
        }

    def close(self):
        """Close the database connection."""
        with self._lock:
            self._conn.close()
