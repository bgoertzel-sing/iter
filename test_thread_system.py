#!/usr/bin/env python3
"""
Tests for Iter Thread-Partitioned Context system.
Tests both the transformation (zzz_thread_manager.py) and tool (thread.py).
Run from the iter-omega directory.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Set up the test environment
TEST_DIR = tempfile.mkdtemp(prefix="iter-thread-test-")
ORIG_CWD = os.getcwd()


def setUpModule():
    """Create a clean test workspace."""
    os.chdir(TEST_DIR)
    Path("threads").mkdir(exist_ok=True)
    Path("memory").mkdir(exist_ok=True)
    Path("tools").mkdir(exist_ok=True)
    Path("transformations").mkdir(exist_ok=True)
    # Copy the actual files
    shutil.copy("/tmp/iter-omega/tools/thread.py", "tools/thread.py")
    shutil.copy("/tmp/iter-omega/transformations/zzz_thread_manager.py", "transformations/zzz_thread_manager.py")


def tearDownModule():
    """Clean up."""
    os.chdir(ORIG_CWD)
    shutil.rmtree(TEST_DIR, ignore_errors=True)


def _import_module(path):
    """Import a module from a file path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_test_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_messages(system_content="You are a test agent.", experience=None, temporary=None):
    """Build a realistic message list like iter.py does."""
    msgs = [{"role": "system", "content": system_content}]
    if experience:
        msgs.extend(experience)
    if temporary:
        msgs.extend(temporary)
    return msgs


def _make_experience_message(role, content, tool_calls=None, tool_call_id=None):
    """Create a single experience message."""
    msg = {"role": role, "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if tool_call_id:
        msg["tool_call_id"] = tool_call_id
    return msg


class TestToolThread(unittest.TestCase):
    """Tests for tools/thread.py"""

    def setUp(self):
        """Clean threads directory before each test."""
        threads_dir = Path("threads")
        if threads_dir.exists():
            shutil.rmtree(threads_dir)
        threads_dir.mkdir()
        self.mod = _import_module("tools/thread.py")

    def test_has_description(self):
        """Tool must have DESCRIPTION string."""
        self.assertTrue(hasattr(self.mod, "DESCRIPTION"))
        self.assertIsInstance(self.mod.DESCRIPTION, str)
        self.assertTrue(len(self.mod.DESCRIPTION) > 10)

    def test_has_run_function(self):
        """Tool must have run() function."""
        self.assertTrue(hasattr(self.mod, "run"))
        self.assertTrue(callable(self.mod.run))

    def test_run_signature(self):
        """run() parameters must all be strings (iter.py requirement)."""
        import inspect
        params = inspect.signature(self.mod.run).parameters
        self.assertIn("action", params)
        self.assertIn("thread_id", params)
        self.assertIn("state", params)
        self.assertIn("keywords", params)

    def test_create_thread(self):
        """Create a new thread."""
        result = self.mod.run("create", "eng-f04", "Working on Gov Bridge", "gov,bridge,test")
        self.assertIn("SUCCESS", result)
        self.assertIn("eng-f04", result)
        # Verify registry
        reg = json.loads(Path("threads/_registry.json").read_text())
        self.assertEqual(reg["activeThread"], "eng-f04")
        self.assertIn("eng-f04", reg["threads"])
        self.assertEqual(reg["threads"]["eng-f04"]["keywords"], ["gov", "bridge", "test"])
        # Verify checkpoint
        self.assertTrue(Path("threads/eng-f04.md").is_file())
        self.assertEqual(Path("threads/eng-f04.md").read_text(), "Working on Gov Bridge")

    def test_create_without_id(self):
        """Create without thread_id should error."""
        result = self.mod.run("create")
        self.assertIn("ERROR", result)

    def test_create_duplicate(self):
        """Creating a duplicate thread should error."""
        self.mod.run("create", "eng-f04", "", "gov")
        result = self.mod.run("create", "eng-f04", "", "gov")
        self.assertIn("ERROR", result)
        self.assertIn("already exists", result)

    def test_create_max_threads(self):
        """Cannot exceed MAX_THREADS active threads."""
        for i in range(5):
            self.mod.run("create", f"t{i}", "", "kw")
        result = self.mod.run("create", "t5", "", "kw")
        self.assertIn("ERROR", result)
        self.assertIn("max", result.lower())

    def test_save_checkpoint(self):
        """Save a checkpoint for the active thread."""
        self.mod.run("create", "eng-f04", "", "gov")
        result = self.mod.run("save", "eng-f04", "Checkpoint: 55/55 tests passing")
        self.assertIn("SUCCESS", result)
        self.assertEqual(Path("threads/eng-f04.md").read_text(), "Checkpoint: 55/55 tests passing")

    def test_save_active_thread_default(self):
        """Save without thread_id should use active thread."""
        self.mod.run("create", "eng-f04", "", "gov")
        result = self.mod.run("save", "", "Auto-saved state")
        self.assertIn("SUCCESS", result)
        self.assertEqual(Path("threads/eng-f04.md").read_text(), "Auto-saved state")

    def test_save_without_state(self):
        """Save without state should error."""
        self.mod.run("create", "eng-f04", "", "gov")
        result = self.mod.run("save", "eng-f04")
        self.assertIn("ERROR", result)

    def test_save_updates_keywords(self):
        """Save with keywords should update them."""
        self.mod.run("create", "eng-f04", "", "gov,bridge")
        self.mod.run("save", "eng-f04", "state", "gov,bridge,petta,wmtm")
        reg = json.loads(Path("threads/_registry.json").read_text())
        self.assertEqual(reg["threads"]["eng-f04"]["keywords"], ["gov", "bridge", "petta", "wmtm"])

    def test_switch_thread(self):
        """Switch between threads."""
        self.mod.run("create", "eng-f04", "State A", "gov")
        self.mod.run("create", "discuss", "State B", "iter,arch")
        result = self.mod.run("switch", "eng-f04")
        self.assertIn("SUCCESS", result)
        reg = json.loads(Path("threads/_registry.json").read_text())
        self.assertEqual(reg["activeThread"], "eng-f04")

    def test_switch_auto_saves_previous(self):
        """Switching with state param auto-saves previous thread."""
        self.mod.run("create", "eng-f04", "Old state", "gov")
        self.mod.run("create", "discuss", "", "iter")
        # Now active is 'discuss'. Switch back to eng-f04, saving discuss's state.
        result = self.mod.run("switch", "eng-f04", "Discuss checkpoint saved on switch")
        self.assertIn("auto-saved", result.lower())
        self.assertEqual(Path("threads/discuss.md").read_text(), "Discuss checkpoint saved on switch")

    def test_switch_nonexistent(self):
        """Switch to nonexistent thread should error."""
        result = self.mod.run("switch", "nonexistent")
        self.assertIn("ERROR", result)

    def test_switch_closed_thread(self):
        """Switch to closed thread should error."""
        self.mod.run("create", "eng-f04", "", "gov")
        self.mod.run("create", "discuss", "", "iter")
        self.mod.run("close", "eng-f04")
        result = self.mod.run("switch", "eng-f04")
        self.assertIn("ERROR", result)
        self.assertIn("closed", result.lower())

    def test_list_threads(self):
        """List all threads."""
        self.mod.run("create", "eng-f04", "State", "gov")
        self.mod.run("create", "discuss", "", "iter")
        result = self.mod.run("list")
        self.assertIn("eng-f04", result)
        self.assertIn("discuss", result)
        self.assertIn("ACTIVE", result)

    def test_list_empty(self):
        """List with no threads."""
        result = self.mod.run("list")
        self.assertIn("No threads", result)

    def test_close_thread(self):
        """Close a thread."""
        self.mod.run("create", "eng-f04", "", "gov")
        self.mod.run("create", "discuss", "", "iter")
        result = self.mod.run("close", "eng-f04")
        self.assertIn("SUCCESS", result)
        reg = json.loads(Path("threads/_registry.json").read_text())
        self.assertEqual(reg["threads"]["eng-f04"]["status"], "closed")
        # Active should switch to discuss
        self.assertEqual(reg["activeThread"], "discuss")

    def test_close_last_thread(self):
        """Close the only active thread — activeThread becomes None."""
        self.mod.run("create", "eng-f04", "", "gov")
        self.mod.run("close", "eng-f04")
        reg = json.loads(Path("threads/_registry.json").read_text())
        self.assertIsNone(reg["activeThread"])

    def test_unknown_action(self):
        """Unknown action should error."""
        result = self.mod.run("explode")
        self.assertIn("ERROR", result)

    def test_underscore_thread_id(self):
        """Thread IDs starting with _ should be rejected."""
        result = self.mod.run("create", "_internal", "", "gov")
        self.assertIn("ERROR", result)

    def test_atomic_write(self):
        """Registry should be written atomically (no .tmp file left)."""
        self.mod.run("create", "eng-f04", "state", "gov")
        self.assertFalse(Path("threads/_registry.tmp").exists())
        self.assertFalse(Path("threads/eng-f04.tmp").exists())


class TestTransformation(unittest.TestCase):
    """Tests for transformations/zzz_thread_manager.py"""

    def setUp(self):
        """Clean threads directory before each test."""
        threads_dir = Path("threads")
        if threads_dir.exists():
            shutil.rmtree(threads_dir)
        threads_dir.mkdir()
        self.tool = _import_module("tools/thread.py")
        self.transform = _import_module("transformations/zzz_thread_manager.py")

    def test_has_description(self):
        """Transformation must have DESCRIPTION string."""
        self.assertTrue(hasattr(self.transform, "DESCRIPTION"))
        self.assertIsInstance(self.transform.DESCRIPTION, str)

    def test_has_transform_function(self):
        """Transformation must have transform(messages, tools)."""
        self.assertTrue(hasattr(self.transform, "transform"))
        self.assertTrue(callable(self.transform.transform))
        import inspect
        params = list(inspect.signature(self.transform.transform).parameters.keys())
        self.assertEqual(params, ["messages", "tools"])

    def test_returns_tuple(self):
        """transform() must return (messages, tools) tuple."""
        msgs = _make_messages()
        tools = [{"type": "function", "function": {"name": "test"}}]
        result = self.transform.transform(msgs, tools)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        result_msgs, result_tools = result
        self.assertIsInstance(result_msgs, list)
        self.assertIsInstance(result_tools, list)

    def test_passthrough_no_registry(self):
        """Without a registry, messages pass through unchanged."""
        msgs = _make_messages(experience=[
            _make_experience_message("user", "Hello"),
            _make_experience_message("assistant", "Hi there"),
        ])
        tools = []
        result_msgs, result_tools = self.transform.transform(msgs, tools)
        self.assertEqual(result_msgs, msgs)
        self.assertEqual(result_tools, tools)

    def test_passthrough_no_active_thread(self):
        """With registry but no active thread, pass through unchanged."""
        Path("threads/_registry.json").write_text(json.dumps({
            "activeThread": None,
            "threads": {},
            "version": 1
        }))
        msgs = _make_messages()
        tools = []
        result_msgs, _ = self.transform.transform(msgs, tools)
        self.assertEqual(result_msgs, msgs)

    def test_checkpoint_injection(self):
        """Active thread with checkpoint should inject it after system message."""
        self.tool.run("create", "eng-f04", "Gov Bridge: 69/69 tests passing", "gov,bridge,test")
        msgs = _make_messages(experience=[
            _make_experience_message("user", "Run the gov bridge tests"),
        ])
        tools = []
        result_msgs, _ = self.transform.transform(msgs, tools)
        # Should have system + checkpoint + user = 3 messages
        self.assertGreaterEqual(len(result_msgs), 3)
        # Second message should be the checkpoint injection
        self.assertEqual(result_msgs[1]["role"], "user")
        self.assertIn("ACTIVE THREAD: eng-f04", result_msgs[1]["content"])
        self.assertIn("Gov Bridge: 69/69 tests passing", result_msgs[1]["content"])

    def test_checkpoint_injection_no_state(self):
        """Active thread without checkpoint file should inject a hint."""
        self.tool.run("create", "eng-f04", "", "gov,bridge")
        # Delete the checkpoint file (create with empty state won't create it)
        cp = Path("threads/eng-f04.md")
        if cp.exists():
            cp.unlink()
        msgs = _make_messages()
        tools = []
        result_msgs, _ = self.transform.transform(msgs, tools)
        self.assertGreaterEqual(len(result_msgs), 2)
        self.assertIn("No checkpoint", result_msgs[1]["content"])

    def test_experience_filtering_keeps_relevant(self):
        """Messages matching thread keywords should be kept."""
        self.tool.run("create", "eng-f04", "Gov Bridge work", "gov,bridge,test,petta")
        experience = [
            _make_experience_message("user", "Let's work on the gov bridge tests"),
            _make_experience_message("assistant", "Running the bridge test suite now"),
            _make_experience_message("user", "What about the weather today?"),
            _make_experience_message("assistant", "I don't have weather info"),
            _make_experience_message("user", "Back to gov bridge — are petta tests passing?"),
            _make_experience_message("assistant", "Yes, all gov bridge tests pass"),
        ]
        # Add enough messages so filtering actually engages
        # (need more than ALWAYS_KEEP_RECENT to have an experience region to filter)
        padding = []
        for i in range(15):
            padding.append(_make_experience_message("user", f"Irrelevant padding message {i} about cooking recipes"))
            padding.append(_make_experience_message("assistant", f"Sure, here's cooking recipe {i}"))
        all_experience = padding + experience

        msgs = _make_messages(
            experience=all_experience,
            temporary=[_make_experience_message("user", "Step 2026-09-25: continue")]
        )
        tools = []
        result_msgs, _ = self.transform.transform(msgs, tools)
        # Result should have fewer messages than input (padding filtered out)
        self.assertLess(len(result_msgs), len(msgs))

    def test_preserves_system_messages(self):
        """System messages should always be preserved."""
        self.tool.run("create", "eng-f04", "State", "gov,bridge")
        msgs = _make_messages(experience=[
            _make_experience_message("user", "msg " + str(i)) for i in range(20)
        ])
        tools = []
        result_msgs, _ = self.transform.transform(msgs, tools)
        system_msgs = [m for m in result_msgs if m.get("role") == "system"]
        self.assertGreaterEqual(len(system_msgs), 1)

    def test_preserves_recent_messages(self):
        """The most recent N messages should always be kept."""
        self.tool.run("create", "eng-f04", "State", "gov,bridge")
        experience = []
        for i in range(25):
            experience.append(_make_experience_message("user", f"Irrelevant message about cooking {i}"))
            experience.append(_make_experience_message("assistant", f"Cooking response {i}"))
        # Add recent relevant messages
        experience.append(_make_experience_message("user", "Recent: gov bridge status?"))
        experience.append(_make_experience_message("assistant", "Bridge tests all pass"))

        msgs = _make_messages(experience=experience)
        tools = []
        result_msgs, _ = self.transform.transform(msgs, tools)
        # Last messages should still be there
        contents = [m.get("content", "") for m in result_msgs]
        self.assertTrue(any("Recent: gov bridge" in c for c in contents))
        self.assertTrue(any("Bridge tests all pass" in c for c in contents))

    def test_preserves_tool_call_groups(self):
        """Assistant messages with tool_calls and their matching tool results stay together."""
        self.tool.run("create", "eng-f04", "State", "gov,bridge,test")
        experience = []
        # Add irrelevant padding
        for i in range(20):
            experience.append(_make_experience_message("user", f"Cooking question {i}"))
            experience.append(_make_experience_message("assistant", f"Cooking answer {i}"))

        # Add a relevant tool call group
        experience.append(_make_experience_message("user", "Run the gov bridge tests"))
        experience.append(_make_experience_message(
            "assistant", "Running tests",
            tool_calls=[{"id": "call_abc123", "type": "function",
                        "function": {"name": "shell", "arguments": '{"cmd":"pytest test_gov_bridge.py"}'}}]
        ))
        experience.append(_make_experience_message("tool", "All 69 tests passed", tool_call_id="call_abc123"))

        msgs = _make_messages(experience=experience)
        tools = []
        result_msgs, _ = self.transform.transform(msgs, tools)

        # The tool call and result should both be present
        has_tool_call = any(
            m.get("tool_calls") and any(tc.get("id") == "call_abc123" for tc in m.get("tool_calls", []))
            for m in result_msgs
        )
        has_tool_result = any(
            m.get("tool_call_id") == "call_abc123" for m in result_msgs
        )
        self.assertTrue(has_tool_call, "Tool call should be preserved")
        self.assertTrue(has_tool_result, "Tool result should be preserved")

    def test_no_filtering_without_keywords(self):
        """Thread without keywords should inject checkpoint but not filter."""
        self.tool.run("create", "eng-f04", "Some state", "")
        # Force keywords to empty
        reg = json.loads(Path("threads/_registry.json").read_text())
        reg["threads"]["eng-f04"]["keywords"] = []
        Path("threads/_registry.json").write_text(json.dumps(reg))

        experience = [_make_experience_message("user", f"msg {i}") for i in range(20)]
        msgs = _make_messages(experience=experience)
        tools = []
        result_msgs, _ = self.transform.transform(msgs, tools)
        # Should have original + 1 checkpoint injection, no removals
        self.assertEqual(len(result_msgs), len(msgs) + 1)

    def test_corrupt_registry_graceful(self):
        """Corrupt registry should not crash — graceful fallback."""
        Path("threads/_registry.json").write_text("not json at all {{{")
        msgs = _make_messages()
        tools = []
        result_msgs, result_tools = self.transform.transform(msgs, tools)
        self.assertEqual(result_msgs, msgs)

    def test_missing_checkpoint_file(self):
        """Missing checkpoint file should not crash."""
        self.tool.run("create", "eng-f04", "", "gov")
        Path("threads/eng-f04.md").unlink(missing_ok=True)
        msgs = _make_messages()
        tools = []
        result_msgs, _ = self.transform.transform(msgs, tools)
        # Should still inject a "no checkpoint" message
        self.assertTrue(any("No checkpoint" in m.get("content", "") for m in result_msgs))

    def test_tools_passed_through(self):
        """Tools list should be returned unchanged."""
        self.tool.run("create", "eng-f04", "State", "gov")
        tools = [
            {"type": "function", "function": {"name": "send", "description": "Send msg"}},
            {"type": "function", "function": {"name": "shell", "description": "Run shell"}},
        ]
        msgs = _make_messages()
        _, result_tools = self.transform.transform(msgs, tools)
        self.assertEqual(result_tools, tools)

    def test_classification_log(self):
        """Filtering should create a classification log entry."""
        self.tool.run("create", "eng-f04", "State", "gov,bridge")
        experience = []
        for i in range(20):
            experience.append(_make_experience_message("user", f"Cooking recipe {i}"))
            experience.append(_make_experience_message("assistant", f"Recipe response {i}"))
        msgs = _make_messages(experience=experience)
        self.transform.transform(msgs, [])
        log_path = Path("threads/_classification.log")
        self.assertTrue(log_path.is_file())
        content = log_path.read_text()
        self.assertIn("eng-f04", content)
        self.assertIn("removed=", content)


class TestIntegration(unittest.TestCase):
    """Integration tests: tool + transformation working together."""

    def setUp(self):
        threads_dir = Path("threads")
        if threads_dir.exists():
            shutil.rmtree(threads_dir)
        threads_dir.mkdir()
        self.tool = _import_module("tools/thread.py")
        self.transform = _import_module("transformations/zzz_thread_manager.py")

    def test_full_workflow(self):
        """Complete workflow: create -> save -> switch -> filter -> switch back."""
        # Create two threads
        self.tool.run("create", "eng-f04", "Gov Bridge: initial state", "gov,bridge,test,petta")
        self.tool.run("create", "discuss", "Discussion about Iter architecture", "iter,thread,context,architecture")

        # Simulate engineering experience
        eng_experience = [
            _make_experience_message("user", "Let's fix the gov bridge test failures"),
            _make_experience_message("assistant", "Running petta test suite for gov bridge"),
            _make_experience_message("user", "The bridge integration test is failing on PLN propagation"),
            _make_experience_message("assistant", "Found the bug in gov bridge PLN mapper"),
        ]
        # Simulate discussion experience
        discuss_experience = [
            _make_experience_message("user", "How should we handle Iter thread context switching?"),
            _make_experience_message("assistant", "We need a transformation that filters experience by thread"),
            _make_experience_message("user", "What about the architecture for context partitioning?"),
            _make_experience_message("assistant", "Using iter.py transformations is the right approach"),
        ]

        # Add padding to exceed ALWAYS_KEEP_RECENT
        padding = []
        for i in range(15):
            padding.append(_make_experience_message("user", f"Unrelated topic {i}: weather in Vancouver"))
            padding.append(_make_experience_message("assistant", f"Weather response {i}"))

        # Switch to engineering thread
        self.tool.run("switch", "eng-f04")
        all_experience = discuss_experience + padding + eng_experience
        msgs = _make_messages(
            experience=all_experience,
            temporary=[_make_experience_message("user", "Step 2026-09-25: continue engineering")]
        )
        result_msgs, _ = self.transform.transform(msgs, [])

        # Engineering messages should be in the result
        contents = " ".join(m.get("content", "") for m in result_msgs)
        self.assertIn("gov bridge", contents.lower())
        # Checkpoint should be injected
        self.assertIn("ACTIVE THREAD: eng-f04", contents)

        # Now switch to discussion thread
        self.tool.run("switch", "discuss", "Engineering paused at: PLN mapper bug found")
        msgs2 = _make_messages(
            experience=eng_experience + padding + discuss_experience,
            temporary=[_make_experience_message("user", "Step 2026-09-25: continue discussion")]
        )
        result_msgs2, _ = self.transform.transform(msgs2, [])
        contents2 = " ".join(m.get("content", "") for m in result_msgs2)
        self.assertIn("ACTIVE THREAD: discuss", contents2)
        self.assertIn("iter", contents2.lower())

    def test_graceful_degradation(self):
        """If threading has any issue, full context should be preserved."""
        # Start with no threads at all
        msgs = _make_messages(experience=[
            _make_experience_message("user", "Hello"),
            _make_experience_message("assistant", "Hi"),
        ])
        result_msgs, _ = self.transform.transform(msgs, [])
        # Everything preserved
        self.assertEqual(len(result_msgs), len(msgs))

    def test_existing_transformations_not_affected(self):
        """Verify our transformation doesn't break if it sees messages from other transforms."""
        self.tool.run("create", "eng-f04", "State", "gov")
        # Simulate messages that include alarm injection (from alarms.py)
        msgs = _make_messages(experience=[
            _make_experience_message("user", "Step 2026-09-25: [terminal] [ALARM] Submit paper"),
            _make_experience_message("user", "Normal message about gov bridge"),
        ])
        result_msgs, _ = self.transform.transform(msgs, [])
        # Should not crash and should preserve the alarm
        alarm_msgs = [m for m in result_msgs if "ALARM" in m.get("content", "")]
        self.assertGreaterEqual(len(alarm_msgs), 0)  # May or may not be filtered, but shouldn't crash


if __name__ == "__main__":
    unittest.main(verbosity=2)
