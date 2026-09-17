"""Test the simulate_incident.py scenario script."""
import pytest
import sys
import os
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TestSimulationScript:
    def test_simulation_runs_successfully(self):
        """simulate_incident.py should run end-to-end without errors."""
        result = subprocess.run(
            [sys.executable, "simulate_incident.py"],
            capture_output=True, text=True, timeout=10,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "SIMULATION COMPLETE" in result.stdout
        assert "Admitted: 3/3" in result.stdout
        assert "publish_redacted_summary" in result.stdout

    def test_simulation_produces_json_summary(self):
        """Script should output JSON summary at end."""
        result = subprocess.run(
            [sys.executable, "simulate_incident.py"],
            capture_output=True, text=True, timeout=10,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        assert "JSON summary:" in result.stdout
        assert '"facts_loaded": 8' in result.stdout
        assert '"gc_decisions": 3' in result.stdout
        assert '"admitted": 3' in result.stdout

    def test_simulation_has_all_phases(self):
        """All 8 phases should be present in output."""
        result = subprocess.run(
            [sys.executable, "simulate_incident.py"],
            capture_output=True, text=True, timeout=10,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        for phase in range(1, 9):
            assert f"Phase {phase}:" in result.stdout, f"Missing phase {phase}"
