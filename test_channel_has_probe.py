"""_channel_has must probe real attributes, not grep source text.

Regression for the Sep 30 19:01 deploy: channels/protocosmo2.py mentioned
receive_request/resume_all inside a guarded `try: from lib import ... /
except ImportError: pass`, the library lacked them, the text probe said yes,
and every receive() failed with AttributeError.
"""
import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = (HERE / "iter.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)

GUARDED = (
    "def receive():\n    return None\n"
    "try:\n"
    "    from _no_such_lib_xyz import receive_request, resume_all, active_request_ids\n"
    "except ImportError:\n"
    "    pass\n"
)
REAL = (
    "def receive_request():\n    return None\n"
    "def close_active(note=None, request_id=None):\n    return False\n"
    "expire_stale = 'not callable'\n"
)


def seg(name):
    node = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(SRC, node)


def worker_probe(channel, names):
    """Run the real iter.py --invoke worker with __has_attrs__ (exits before the main loop)."""
    with tempfile.TemporaryDirectory() as tmp:
        res, pay = Path(tmp) / "r.json", Path(tmp) / "p.json"
        pay.write_text(json.dumps({"args": list(names), "kwargs": {}}))
        subprocess.run([sys.executable, str(HERE / "iter.py"), "--invoke", str(channel),
                        "__has_attrs__", str(res), str(pay)],
                       cwd=tmp, timeout=60, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return json.loads(res.read_text())


class WorkerProbeTest(unittest.TestCase):
    def test_guarded_import_of_missing_hooks_reports_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = Path(tmp) / "pc.py"
            ch.write_text(GUARDED)
            out = worker_probe(ch, ["receive", "receive_request", "resume_all", "active_request_ids"])
        self.assertEqual(out, {"ok": True, "result": {
            "receive": True, "receive_request": False, "resume_all": False, "active_request_ids": False}})

    def test_non_callable_attribute_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = Path(tmp) / "pc.py"
            ch.write_text(REAL)
            out = worker_probe(ch, ["receive_request", "close_active", "expire_stale"])
        self.assertEqual(out["result"], {"receive_request": True, "close_active": True, "expire_stale": False})


class ChannelHasTest(unittest.TestCase):
    def make(self, invoke):
        ns = {"Path": Path, "invoke_dynamic": invoke, "_channel_probe_cache": {},
              "_CHANNEL_PROBE_NAMES": ("receive_request", "resume_all", "active_request_ids",
                                       "close_active", "expire_stale")}
        exec(seg("_channel_has"), ns)
        return ns

    def test_uses_probe_and_caches_until_file_changes(self):
        calls = []
        answers = {"receive_request": False}

        def invoke(path, function, *names):
            calls.append(function)
            return {"ok": True, "result": {n: answers.get(n, False) for n in names}}
        ns = self.make(invoke)
        with tempfile.TemporaryDirectory() as tmp:
            ch = Path(tmp) / "pc.py"
            ch.write_text(GUARDED)  # mentions receive_request in text
            self.assertFalse(ns["_channel_has"](ch, "receive_request"))
            self.assertFalse(ns["_channel_has"](ch, "resume_all"))
            self.assertEqual(calls, ["__has_attrs__"])  # one probe, cached
            answers["receive_request"] = True
            ch.write_text(REAL + "# changed\n")
            self.assertTrue(ns["_channel_has"](ch, "receive_request"))
            self.assertEqual(calls, ["__has_attrs__", "__has_attrs__"])

    def test_probe_failure_or_missing_file_is_false(self):
        ns = self.make(lambda p, f, *a: {"ok": False, "error": "SyntaxError: boom"})
        with tempfile.TemporaryDirectory() as tmp:
            ch = Path(tmp) / "pc.py"
            ch.write_text("def receive_request(:\n")
            self.assertFalse(ns["_channel_has"](ch, "receive_request"))
            self.assertFalse(ns["_channel_has"](Path(tmp) / "missing.py", "receive_request"))

    def test_uncached_name_triggers_reprobe(self):
        calls = []

        def invoke(path, function, *names):
            calls.append(names)
            return {"ok": True, "result": {n: n == "custom_hook" for n in names}}
        ns = self.make(invoke)
        with tempfile.TemporaryDirectory() as tmp:
            ch = Path(tmp) / "pc.py"
            ch.write_text(REAL)
            self.assertFalse(ns["_channel_has"](ch, "close_active"))
            self.assertTrue(ns["_channel_has"](ch, "custom_hook"))
            self.assertIn("custom_hook", calls[-1])


if __name__ == "__main__":
    unittest.main()
