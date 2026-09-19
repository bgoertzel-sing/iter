"""Live Tool Bridge: integrates WMTM orchestrator with Iter agent tools.

The Iter environment provides `goalchainer_decide` and `wmtm_derive` as callable
tools (not Python imports). This module provides:

1. parse_goalchainer_output(text): Parse goalchainer_decide text output → dict
2. parse_wmtm_derive_output(text): Parse wmtm_derive confirmation → dict
3. LiveGoalChainerAdapter: Drop-in replacement for run_goalchainer that uses
   the real goalchainer_decide tool output format.

Usage in Iter agent context:
    from wmtm.live_tool_bridge import parse_goalchainer_output
    result = parse_goalchainer_output(goalchainer_decide(request="..."))
    candidates = goalchainer_result_to_candidates(result, source_ids=[...])
"""
from __future__ import annotations

import re


def parse_goalchainer_output(text: str) -> dict:
    """Parse the text output of goalchainer_decide into a dict.

    Expected format:
        Decision: <action_id> - <label>
        Status: <status>

        Ranked actions:
          <action_id>: <label> - <status> (score=<float>)
          ...

        WMTM evidence: <N> items fed to GoalChainer

    Returns dict with keys:
        - decision: {action_id, label, status}
        - decisions: [{action_id, label, status, score, motivation}]
        - evidence_count: int
    """
    result = {
        "decision": {},
        "decisions": [],
        "evidence_count": 0,
    }

    # Parse top decision
    dec_match = re.search(r"Decision:\s+(\S+)\s+-\s+(.+)", text)
    if dec_match:
        result["decision"]["action_id"] = dec_match.group(1)
        result["decision"]["label"] = dec_match.group(2).strip()

    status_match = re.search(r"Status:\s+(\S+)", text)
    if status_match:
        result["decision"]["status"] = status_match.group(1).strip()

    # Parse ranked actions
    action_pattern = re.compile(
        r"^\s+(\S+):\s+(.+?)\s+-\s+(\S+)\s+\(score=([\d.]+)\)",
        re.MULTILINE,
    )
    for m in action_pattern.finditer(text):
        action_id = m.group(1)
        label = m.group(2).strip()
        status = m.group(3).strip()
        score = float(m.group(4))
        result["decisions"].append({
            "action_id": action_id,
            "label": label,
            "status": status,
            "score": score,
            "motivation": "",
        })

    # Parse evidence count
    ev_match = re.search(r"WMTM evidence:\s+(\d+)\s+items", text)
    if ev_match:
        result["evidence_count"] = int(ev_match.group(1))

    return result


def parse_wmtm_derive_output(text: str) -> dict:
    """Parse wmtm_derive tool output confirmation.

    Expected: confirmation text containing the derived note and source IDs.

    Returns dict with keys:
        - success: bool
        - note: str
        - source_ids: list
    """
    result = {"success": False, "note": "", "source_ids": []}

    if "registered" in text.lower() or "derived" in text.lower() or "success" in text.lower():
        result["success"] = True

    result["note"] = text.strip()
    return result


class LiveGoalChainerAdapter:
    """Adapter that wraps goalchainer_decide tool output for WMTM integration.

    In the Iter agent environment, goalchainer_decide returns text.
    This adapter parses that text and returns the dict format expected
    by goalchainer_result_to_candidates.

    Usage:
        adapter = LiveGoalChainerAdapter()
        # In Iter context: raw = goalchainer_decide(request="...")
        raw = goalchainer_decide(request="...")  # agent tool call
        result_dict = adapter.parse(raw)
        candidates = goalchainer_result_to_candidates(result_dict, source_ids=[...])
    """

    def __init__(self):
        """Initialize live tool bridge with empty parse state."""
        self.last_raw_output = None
        self.last_parsed = None

    def parse(self, raw_text: str) -> dict:
        """Parse raw goalchainer_decide output into dict format."""
        self.last_raw_output = raw_text
        self.last_parsed = parse_goalchainer_output(raw_text)
        return self.last_parsed

    def to_candidates(self, raw_text: str, source_ids: list = None) -> list:
        """One-step: parse raw output → InferenceCandidates."""
        from wmtm.goalchainer_bridge import goalchainer_result_to_candidates
        result = self.parse(raw_text)
        if source_ids is None:
            source_ids = []
        return goalchainer_result_to_candidates(result, source_ids=source_ids)
