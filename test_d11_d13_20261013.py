"""2026-10-13, VM2 gate-2 findings on the laptop: expiry spares owned requests (D11);
a failed call's roll-back keeps branch entries merged after the checkpoint (D13)."""
import ast
import contextlib
import io
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SRC = (HERE / "iter.py").read_text()
TREE = ast.parse(SRC)


def seg(name):
    return ast.get_source_segment(SRC, next(n for n in TREE.body
                                            if isinstance(n, ast.FunctionDef) and n.name == name))


CHANNEL = ("def receive_request(): pass\ndef close_active(note='', request_id=None): pass\n"
           "def expire_stale(s, note='', now=None, keep=()): pass\n")


class ExpireSparesOwnedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        Path("channels").mkdir()
        Path("channels/pc.py").write_text(CHANNEL)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def load(self, foreground=None, branches=(), results=None):
        calls, results = [], results or {}

        def invoke_dynamic(path, function, *args, **kwargs):
            calls.append((function, args, kwargs))
            return results.get(function, {"ok": True, "result": False})
        ns = {"Path": Path, "os": os, "invoke_dynamic": invoke_dynamic, "ITER_REQUEST_TIMEOUT": 3600.0,
              "ITER_CONCURRENCY_ENABLED": True, "ITER_REQUEST_BINDING": True,
              "_branch_lock": threading.Lock(),
              "_foreground_request_ids": {"pc": foreground} if foreground else {},
              "_active_branches": {i: SimpleNamespace(request_ids={"pc": rid}) for i, rid in enumerate(branches)},
              "background_branch_active": lambda: bool(branches),
              "_channel_has": lambda path, name: f"def {name}(" in Path(path).read_text()}
        exec(seg("_owned_request_ids"), ns)
        exec(seg("close_unfinished_requests"), ns)
        return ns["close_unfinished_requests"], calls

    def test_foreground_and_branch_requests_are_kept(self):
        fn, calls = self.load(foreground="f" * 64, branches=["b" * 64, "c" * 64])
        fn()
        self.assertEqual(calls[0], ("expire_stale", (3600.0,), {"keep": ["b" * 64, "c" * 64, "f" * 64]}))

    def test_nothing_owned_calls_the_old_signature(self):
        fn, calls = self.load()
        fn()
        self.assertEqual(calls[0], ("expire_stale", (3600.0,), {}))

    def test_channel_without_keep_skips_expiry_but_still_closes(self):
        fn, calls = self.load(foreground="f" * 64, results={
            "expire_stale": {"ok": False, "error": "TypeError: expire_stale() got an unexpected keyword argument 'keep'"}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn()
        self.assertIn("STALE_REQUEST_EXPIRE_SKIPPED: channel=pc", out.getvalue())
        self.assertEqual([c[0] for c in calls], ["expire_stale", "close_active"])
        self.assertEqual(calls[1][2], {"request_id": "f" * 64})


class RollbackKeepsMergedTest(unittest.TestCase):
    def rollback(self):
        ns = {}
        exec(seg("_rollback_keeping_merged"), ns)
        return ns["_rollback_keeping_merged"]

    def test_merged_entries_survive_and_failed_turn_entries_do_not(self):
        before = [{"role": "user", "content": "old"}]
        user = {"role": "user", "content": "new input"}
        merged = [{"role": "assistant", "content": "branch answer"}, {"role": "tool", "content": "branch tool"}]
        failed = {"role": "assistant", "content": "half a turn"}
        experience = before + [user] + merged + [failed]
        self.assertEqual(self.rollback()(experience, 2, merged), before + [user] + merged)
        self.assertEqual(self.rollback()(experience, 1, merged), before + merged)  # terminal: input dropped
        self.assertEqual(self.rollback()(experience, 2, []), before + [user])

    def test_equal_but_distinct_entries_are_not_kept_by_value(self):
        merged = [{"role": "tool", "content": "x"}]
        experience = [{"role": "tool", "content": "x"}]  # same value, not the merged object
        self.assertEqual(self.rollback()(experience, 0, merged), [])

    def test_main_loop_records_drain_b_and_uses_it_on_both_rollbacks(self):
        self.assertRegex(SRC, r"merged_from = len\(experience\)\n\s+drain_merge_queue\(\)\n\s+"
                              r"merged_after_checkpoint \+= experience\[merged_from:\]")
        self.assertIsNone(re.search(r"experience = experience\[:(history|turn)_checkpoint\]", SRC))
        for checkpoint in ("turn_checkpoint", "history_checkpoint"):
            if checkpoint == "turn_checkpoint" and "turn_checkpoint" not in SRC:
                continue  # the reference branch has no terminal-failure roll-back
            self.assertIn(f"_rollback_keeping_merged(experience, {checkpoint}, merged_after_checkpoint)", SRC)


if __name__ == "__main__":
    unittest.main()
