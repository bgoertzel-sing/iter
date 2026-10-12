"""tools/send.py: final flag passthrough and backward compatibility."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("send_tool", HERE / "tools" / "send.py")
send_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(send_tool)

NEW = '''CALLS = []
def send(content, final=True):
    import json, pathlib
    pathlib.Path("calls.jsonl").open("a").write(json.dumps([content, final]) + "\\n")
'''
OLD = '''def send(content):
    import json, pathlib
    pathlib.Path("calls.jsonl").open("a").write(json.dumps([content, None]) + "\\n")
'''


class SendToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        Path("channels").mkdir()
        Path("channels/new.py").write_text(NEW)
        Path("channels/old.py").write_text(OLD)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def calls(self):
        import json
        return [json.loads(l) for l in Path("calls.jsonl").read_text().splitlines()]

    def test_default_is_final_and_old_call_shape(self):
        self.assertEqual(send_tool.run("old", "hi"), "SUCCESS")
        self.assertEqual(send_tool.run("new", "hi"), "SUCCESS")
        self.assertEqual(self.calls(), [["hi", None], ["hi", True]])

    def test_progress_passthrough(self):
        self.assertIn("progress", send_tool.run("new", "step", final=False))
        self.assertIn("progress", send_tool.run("new", "step", final="false"))
        self.assertEqual(self.calls(), [["step", False], ["step", False]])

    def test_progress_on_old_channel_is_refused_not_sent(self):
        self.assertIn("does not support", send_tool.run("old", "step", final=False))
        self.assertFalse(Path("calls.jsonl").exists())

    def test_bad_final_value(self):
        self.assertIn("final must be", send_tool.run("new", "x", final="maybe"))

    def test_unknown_channel_unchanged(self):
        self.assertEqual(send_tool.run("iter", "x"), "Unknown channel: iter")


if __name__ == "__main__":
    unittest.main()
