"""Tests for control/observation.py -- observation collector."""
import json
import os
import tempfile
import pytest
from control.types import Observation, content_digest, now_iso
from control.observation import ObservationCollector


class TestCollect:
    def test_basic_collect(self):
        col = ObservationCollector(schema_version="v1")
        obs = col.collect(
            source="shard_status", collection_method="file_read",
            state_revision=1, content={"shard": "g1014", "status": "running"},
        )
        assert obs.observation_id.startswith("obs_")
        assert obs.source == "shard_status"
        assert obs.schema_version == "v1"
        assert obs.state_revision == 1
        assert len(obs.content_digest) == 64
        assert obs.content_digest == content_digest({"shard": "g1014", "status": "running"})

    def test_collect_with_evidence_ref(self):
        col = ObservationCollector()
        obs = col.collect(
            source="test", collection_method="file_read",
            state_revision=5, content={"x": 1},
            raw_evidence_ref="/tmp/evidence.json",
        )
        assert obs.raw_evidence_ref == "/tmp/evidence.json"

    def test_collect_with_custom_id(self):
        col = ObservationCollector()
        obs = col.collect(
            source="s", collection_method="m", state_revision=1,
            content={"a": 1}, observation_id="custom_id",
        )
        assert obs.observation_id == "custom_id"

    def test_collect_with_digest_override(self):
        col = ObservationCollector()
        obs = col.collect(
            source="s", collection_method="m", state_revision=1,
            content={"a": 1}, content_digest_override="custom_digest",
        )
        assert obs.content_digest == "custom_digest"


class TestCollectFromFile:
    def test_json_file(self):
        col = ObservationCollector()
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"key": "value", "num": 42}, f)
        obs = col.collect_from_file(path, "config_read", state_revision=1)
        assert obs.source == "config_read"
        assert obs.collection_method == "file_read"
        assert obs.raw_evidence_ref == path
        assert obs.content["key"] == "value"
        assert len(obs.content_digest) == 64
        os.unlink(path)

    def test_non_json_file(self):
        col = ObservationCollector()
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write("plain text data")
        obs = col.collect_from_file(path, "log_read", state_revision=1)
        assert "file_sha256" in obs.content
        assert "size_bytes" in obs.content
        assert obs.content["size_bytes"] == len(b"plain text data")
        os.unlink(path)

    def test_missing_file_raises(self):
        col = ObservationCollector()
        with pytest.raises(FileNotFoundError):
            col.collect_from_file("/nonexistent/path", "test", state_revision=1)


class TestRegisteredCollectors:
    def test_register_and_collect(self):
        col = ObservationCollector()
        col.register_source("uptime", lambda: {"uptime": 3600, "load": 0.5})
        obs = col.collect_from_registered("uptime", state_revision=1)
        assert obs.source == "uptime"
        assert obs.collection_method == "registered_fn"
        assert obs.content["uptime"] == 3600

    def test_unregistered_source_raises(self):
        col = ObservationCollector()
        with pytest.raises(ValueError, match="No registered collector"):
            col.collect_from_registered("unknown", state_revision=1)


class TestValidateObservation:
    def test_valid_observation(self):
        col = ObservationCollector(schema_version="v1")
        obs = col.collect(
            source="s", collection_method="m", state_revision=1,
            content={"x": 1},
        )
        assert col.validate_observation(obs) is True

    def test_wrong_schema_version(self):
        col = ObservationCollector(schema_version="v2")
        obs = Observation(
            observation_id="o1", source="s", collection_method="m",
            timestamp=now_iso(), state_revision=1, schema_version="v1",
            content_digest=content_digest({"x": 1}), raw_evidence_ref="",
            content={"x": 1},
        )
        assert col.validate_observation(obs) is False

    def test_negative_revision(self):
        col = ObservationCollector()
        obs = Observation(
            observation_id="o1", source="s", collection_method="m",
            timestamp=now_iso(), state_revision=-1, schema_version="v1",
            content_digest=content_digest({"x": 1}), raw_evidence_ref="",
            content={"x": 1},
        )
        assert col.validate_observation(obs) is False

    def test_empty_content(self):
        col = ObservationCollector()
        obs = Observation(
            observation_id="o1", source="s", collection_method="m",
            timestamp=now_iso(), state_revision=1, schema_version="v1",
            content_digest=content_digest({}), raw_evidence_ref="",
            content={},
        )
        assert col.validate_observation(obs) is False

    def test_wrong_digest(self):
        col = ObservationCollector(schema_version="v1")
        obs = Observation(
            observation_id="o1", source="s", collection_method="m",
            timestamp=now_iso(), state_revision=1, schema_version="v1",
            content_digest="wrong_digest", raw_evidence_ref="",
            content={"x": 1},
        )
        assert col.validate_observation(obs) is False

    def test_zero_revision_valid(self):
        col = ObservationCollector(schema_version="v1")
        obs = col.collect(
            source="s", collection_method="m", state_revision=0,
            content={"x": 1},
        )
        assert col.validate_observation(obs) is True


class TestSchemaVersion:
    def test_default(self):
        col = ObservationCollector()
        assert col.schema_version == "v1"

    def test_custom(self):
        col = ObservationCollector(schema_version="v2")
        assert col.schema_version == "v2"
