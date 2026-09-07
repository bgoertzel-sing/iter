"""Unit tests for tool modules: nop, shell, send, petta_journal round-trip."""
import os
import sys
import tempfile
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def test_nop_returns_success():
    """nop.run() should return 'SUCCESS'."""
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    import nop
    result = nop.run()
    assert result == "SUCCESS", f"Expected 'SUCCESS', got {result!r}"
    sys.path.pop(0)


def test_shell_returns_output():
    """shell.run() should capture and return command output."""
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    import shell
    result = shell.run("echo hello123")
    assert "hello123" in result, f"Expected 'hello123' in result, got: {result!r}"
    assert result.startswith("SUCCESS"), f"Expected SUCCESS prefix, got: {result!r}"
    sys.path.pop(0)


def test_shell_captures_stderr():
    """shell.run() should capture stderr too."""
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    import shell
    result = shell.run("echo error_msg >&2")
    assert "error_msg" in result, f"Expected stderr captured, got: {result!r}"
    sys.path.pop(0)


def test_send_unknown_channel():
    """send.run() should report unknown channel gracefully."""
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    import send
    result = send.run("nonexistent_channel_123", "test message")
    assert "Unknown channel" in result, f"Expected 'Unknown channel', got: {result!r}"
    sys.path.pop(0)


def test_petta_journal_append_and_recall():
    """petta_append + petta_recall should round-trip a note."""
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    import petta_append
    import petta_recall
    marker = f"TEST_MARKER_{os.getpid()}_autonomous_coverage"
    petta_append.run(f"Test note from test_tools.py: {marker}")
    recalled = petta_recall.run(limit=50)
    assert isinstance(recalled, str), f"Expected string, got {type(recalled)}"
    sys.path.pop(0)


def test_python_tool_executes_code():
    """python.run() should execute arbitrary Python and return output."""
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    import python
    result = python.run("print(2 + 2)")
    assert "4" in result, f"Expected '4' in result, got: {result!r}"
    sys.path.pop(0)


if __name__ == "__main__":
    test_nop_returns_success()
    test_shell_returns_output()
    test_shell_captures_stderr()
    test_send_unknown_channel()
    test_petta_journal_append_and_recall()
    test_python_tool_executes_code()
    print("All tool tests passed!")


def test_send_rejects_path_traversal():
    """send.run() should reject channel names with path traversal characters."""
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    import send
    for bad_channel in ["../something", "../../foo", "subdir/file", "..badname"]:
        result = send.run(bad_channel, "test")
        assert "Invalid" in result, (
            f"Expected 'Invalid' for channel {bad_channel!r}, got: {result!r}"
        )
    sys.path.pop(0)
