"""Tests for wmtm.live_tool_bridge: parsing real goalchainer_decide output."""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wmtm.live_tool_bridge import (
    parse_goalchainer_output,
    parse_wmtm_derive_output,
    LiveGoalChainerAdapter,
)
from wmtm.goalchainer_bridge import goalchainer_result_to_candidates


# Real output from goalchainer_decide tool (captured 2026-09-17)
REAL_GC_OUTPUT = """Decision: publish_redacted_summary - Publish redacted summary
Status: recommended

Ranked actions:
  publish_redacted_summary: Publish redacted summary - recommended (score=1.010108)
  publish_raw_log: Publish raw incident log - recommended (score=0.839738)
  hold_external_update: Hold external update - weak (score=0.385841)

WMTM evidence: 16 items fed to GoalChainer"""


class TestParseGoalChainerOutput:
    def test_parses_top_decision(self):
        result = parse_goalchainer_output(REAL_GC_OUTPUT)
        assert result["decision"]["action_id"] == "publish_redacted_summary"
        assert "Publish redacted summary" in result["decision"]["label"]
        assert result["decision"]["status"] == "recommended"

    def test_parses_ranked_actions(self):
        result = parse_goalchainer_output(REAL_GC_OUTPUT)
        assert len(result["decisions"]) == 3
        assert result["decisions"][0]["action_id"] == "publish_redacted_summary"
        assert result["decisions"][0]["status"] == "recommended"
        assert abs(result["decisions"][0]["score"] - 1.010108) < 0.001

    def test_parses_all_scores(self):
        result = parse_goalchainer_output(REAL_GC_OUTPUT)
        scores = [d["score"] for d in result["decisions"]]
        assert scores[0] > scores[1] > scores[2]

    def test_parses_evidence_count(self):
        result = parse_goalchainer_output(REAL_GC_OUTPUT)
        assert result["evidence_count"] == 16

    def test_empty_input(self):
        result = parse_goalchainer_output("")
        assert result["decisions"] == []
        assert result["evidence_count"] == 0

    def test_malformed_input(self):
        result = parse_goalchainer_output("Some random text without structure")
        assert result["decisions"] == []
        assert result["decision"] == {}

    def test_status_variations(self):
        text = """Decision: act_now - Do something
Status: obligated

Ranked actions:
  act_now: Do something - obligated (score=0.95)
  do_nothing: Wait - forbidden (score=0.1)

WMTM evidence: 5 items fed to GoalChainer"""
        result = parse_goalchainer_output(text)
        assert result["decision"]["status"] == "obligated"
        assert result["decisions"][0]["status"] == "obligated"
        assert result["decisions"][1]["status"] == "forbidden"


class TestParseWMTMDeriveOutput:
    def test_success_detection(self):
        text = "Derived belief registered successfully."
        result = parse_wmtm_derive_output(text)
        assert result["success"] is True

    def test_failure_detection(self):
        text = "Error: could not register."
        result = parse_wmtm_derive_output(text)
        assert result["success"] is False

    def test_note_captured(self):
        text = "Registered derived belief: the sky is blue."
        result = parse_wmtm_derive_output(text)
        assert "the sky is blue" in result["note"]


class TestLiveGoalChainerAdapter:
    def test_parse_returns_dict(self):
        adapter = LiveGoalChainerAdapter()
        result = adapter.parse(REAL_GC_OUTPUT)
        assert isinstance(result, dict)
        assert "decisions" in result
        assert len(result["decisions"]) == 3

    def test_to_candidates_produces_inference_candidates(self):
        adapter = LiveGoalChainerAdapter()
        candidates = adapter.to_candidates(
            REAL_GC_OUTPUT,
            source_ids=["f1", "f2"],
        )
        assert len(candidates) == 3
        assert candidates[0].inference_type == "goalchainer_decision"
        assert "publish_redacted_summary" in candidates[0].content
        assert candidates[0].confidence > 0.5  # score was 1.01, clamped to 1.0
        assert "f1" in candidates[0].derived_from

    def test_to_candidates_sti_boost_for_recommended(self):
        adapter = LiveGoalChainerAdapter()
        candidates = adapter.to_candidates(REAL_GC_OUTPUT, source_ids=[])
        # First two are recommended, third is weak
        assert candidates[0].initial_sti > candidates[2].initial_sti

    def test_last_raw_output_stored(self):
        adapter = LiveGoalChainerAdapter()
        adapter.parse(REAL_GC_OUTPUT)
        assert adapter.last_raw_output == REAL_GC_OUTPUT
        assert adapter.last_parsed is not None

    def test_end_to_end_with_goalchainer_result_to_candidates(self):
        """Verify parsed output works with goalchainer_result_to_candidates."""
        result = parse_goalchainer_output(REAL_GC_OUTPUT)
        candidates = goalchainer_result_to_candidates(
            result, source_ids=["e1", "e2", "e3"],
        )
        assert len(candidates) == 3
        # Verify all have provenance
        for c in candidates:
            assert "e1" in c.derived_from
            assert c.inference_type == "goalchainer_decision"
