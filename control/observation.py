"""Observation collector for goal-driven iter control.

Gathers state snapshots from the environment, computes content digests,
validates schema versions, and emits immutable Observation records.

Safety invariants:
- I7: No memory-to-actuation. Observations are pure data captures.
- I3: Observations carry the revision of the state they describe.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from control.types import Observation, content_digest, now_iso


class ObservationCollector:
    """Collects observations from the environment.

    Each observation is an immutable record with a content digest
    computed from the canonical JSON of its content field.
    """

    def __init__(self, schema_version: str = "v1"):
        self._schema_version = schema_version
        self._collectors: dict[str, callable] = {}

    def register_source(self, source_name: str, collector_fn: callable):
        """Register a collector function for a named source."""
        self._collectors[source_name] = collector_fn

    def collect(
        self,
        source: str,
        collection_method: str,
        state_revision: int,
        content: dict[str, Any],
        raw_evidence_ref: str = "",
        observation_id: str = "",
        content_digest_override: str = "",
    ) -> Observation:
        """Create an Observation record.

        The content digest is computed automatically from the content
        if not overridden. The schema version is stamped from the
        collector's configured version.
        """
        digest = content_digest_override or content_digest(content)
        return Observation(
            observation_id=observation_id,
            source=source,
            collection_method=collection_method,
            timestamp=now_iso(),
            state_revision=state_revision,
            schema_version=self._schema_version,
            content_digest=digest,
            raw_evidence_ref=raw_evidence_ref,
            content=content,
        )

    def collect_from_file(
        self, file_path: str, source: str, state_revision: int
    ) -> Observation:
        """Collect an observation from a file on disk.

        Reads the file, parses JSON if possible, and creates an
        Observation with the file path as raw_evidence_ref.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Evidence file not found: {file_path}")

        with open(file_path, "rb") as f:
            raw_bytes = f.read()

        # Compute file digest for integrity
        file_digest = hashlib.sha256(raw_bytes).hexdigest()

        try:
            content = json.loads(raw_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Non-JSON file: store raw hash as content
            content = {"file_sha256": file_digest, "size_bytes": len(raw_bytes)}

        return Observation(
            observation_id="",
            source=source,
            collection_method="file_read",
            timestamp=now_iso(),
            state_revision=state_revision,
            schema_version=self._schema_version,
            content_digest=file_digest,
            raw_evidence_ref=file_path,
            content=content,
        )

    def collect_from_registered(self, source: str, state_revision: int) -> Observation:
        """Collect using a previously registered collector function."""
        if source not in self._collectors:
            raise ValueError(f"No registered collector for source '{source}'")

        content = self._collectors[source]()
        return self.collect(
            source=source,
            collection_method="registered_fn",
            state_revision=state_revision,
            content=content,
        )

    def validate_observation(self, obs: Observation) -> bool:
        """Validate that an observation is well-formed.

        Checks:
        - content_digest matches recomputed digest
        - schema_version matches collector version
        - state_revision is positive
        - content is a non-empty dict
        """
        if obs.state_revision < 0:
            return False
        if not isinstance(obs.content, dict) or len(obs.content) == 0:
            return False
        if obs.schema_version != self._schema_version:
            return False
        # Verify digest matches content
        expected = content_digest(obs.content)
        if obs.content_digest != expected:
            return False
        return True

    @property
    def schema_version(self) -> str:
        return self._schema_version
