"""iter.py close_unfinished_requests: expire_stale/close_active turn-end hook."""
import ast
import os
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = (HERE / "iter.py").read_text()
FUNC = next(n for n in ast.parse(SRC).body
            if isinstance(n, ast.FunctionDef) and n.name == "close_unfinished_requests")
FUNC_SRC = ast.get_source_segment(SRC, FUNC)

MULTI = "def send(c, final=True): pass\ndef close_active(): pass\ndef expire_stale(s): pass\n"
OLD = "def send(c): pass\n"


def load(timeout=3600.0, concurrency=False, branch=None, results=None):
    calls = []
    results = results or {}

    def invoke_dynamic(path, function, *args):
        calls.append((Path(path).stem, function, args))
        return results.get(function, {"ok": True, "result": False})

    ns = {"Path": Path, "os": os, "invoke_dynamic": invoke_dynamic,
          "ITER_REQUEST_TIMEOUT": timeout, "ITER_CONCURRENCY_ENABLED": concurrency,
          "_branch_lock": threading.Lock(), "_active_branch": branch}
    exec(FUNC_SRC, ns)
    return ns["close_unfinished_requests"], calls


class CloseUnfinishedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        Path("channels").mkdir()
        Path("channels/multi.py").write_text(MULTI)
        Path("channels/old.py").write_text(OLD)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_old_channel_untouched_and_order(self):
        fn, calls = load()
        fn()
        self.assertEqual(calls, [("multi", "expire_stale", (3600.0,)), ("multi", "close_active", ())])

    def test_branch_active_only_expires(self):
        fn, calls = load(concurrency=True, branch=object())
        fn()
        self.assertEqual(calls, [("multi", "expire_stale", (3600.0,))])

    def test_timeout_zero_disables_expiry(self):
        fn, calls = load(timeout=0)
        fn()
        self.assertEqual(calls, [("multi", "close_active", ())])

    def test_expired_request_skips_close_active(self):
        fn, calls = load(results={"expire_stale": {"ok": True, "result": True}})
        fn()
        self.assertEqual(calls, [("multi", "expire_stale", (3600.0,))])

    def test_expire_failure_still_closes(self):
        fn, calls = load(results={"expire_stale": {"ok": False, "error": "boom"}})
        fn()
        self.assertEqual([c[1] for c in calls], ["expire_stale", "close_active"])


if __name__ == "__main__":
    unittest.main()
