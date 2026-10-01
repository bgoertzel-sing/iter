"""Hung-call fix: an in-flight branch LLM call is not dropped by the idle deadline,
and a promoted branch retries a timed-out call (bg-89ff37ae).

iter.py runs its main loop at import time, so never `import iter` here;
extract the functions with ast and exec them in a small namespace.
"""
import ast
import os
import queue
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SRC = (HERE / "iter.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def load(*names, **extra):
    segs = []
    for name in names:
        node = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
        segs.append(ast.get_source_segment(SRC, node))
    ns = {"os": os, "time": time, "threading": threading, "queue": queue,
          "LLM_TIMEOUT": 1, "BG_LLM_ATTEMPTS": 2, "BG_RETRY_BACKOFF_CAP": 0,
          "RETRY_BACKOFF_BASE": 0, "BG_INFLIGHT_LIMIT": 1260, "BACKGROUND_DEADLINE": 300,
          "_llm_call_semaphore": threading.BoundedSemaphore(4),
          "retryable_provider_error": lambda e: "timed out" in str(e),
          "provider_quota_exhausted": lambda e: "quota" in str(e),
          **extra}
    exec("\n\n".join(segs), ns)
    return ns


def client_with(create):
    class Completions:
        pass
    Completions.create = staticmethod(create)

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()
    return Client()


def flaky(failures, error="Request timed out."):
    calls = {"n": 0}

    def create(**kw):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise RuntimeError(error)
        return "ok"
    return create, calls


class AttemptsParseTest(unittest.TestCase):
    def test_default_invalid_and_clamp(self):
        f = load("_bg_llm_attempts")["_bg_llm_attempts"]
        old = os.environ.pop("ITER_BG_LLM_ATTEMPTS", None)
        try:
            self.assertEqual(f(), 2)
        finally:
            if old is not None:
                os.environ["ITER_BG_LLM_ATTEMPTS"] = old
        self.assertEqual([f("abc"), f("0"), f("1"), f("3"), f("99")], [2, 1, 1, 3, 5])


class RetryTest(unittest.TestCase):
    def ns(self, **extra):
        return load("_capped_llm_create", "_bg_llm_create", **extra)

    def test_promoted_branch_retries_timeout(self):
        ns = self.ns()
        create, calls = flaky(1)
        rc = {"branch_id": "bg-test"}
        self.assertEqual(ns["_bg_llm_create"](client_with(create), rc, model="m"), "ok")
        self.assertEqual(calls["n"], 2)
        self.assertNotIn("llm_inflight_since", rc)
        self.assertIn("last_progress_at", rc)

    def test_retries_bounded_by_attempts(self):
        ns = self.ns(BG_LLM_ATTEMPTS=3)
        create, calls = flaky(10)
        with self.assertRaises(RuntimeError):
            ns["_bg_llm_create"](client_with(create), {"branch_id": "bg-test"}, model="m")
        self.assertEqual(calls["n"], 3)

    def test_not_promoted_no_retry(self):
        ns = self.ns()
        create, calls = flaky(1)
        with self.assertRaises(RuntimeError):
            ns["_bg_llm_create"](client_with(create), {}, model="m")
        self.assertEqual(calls["n"], 1)

    def test_non_retryable_and_quota_no_retry(self):
        for err in ("bad request", "timed out: quota exceeded"):
            ns = self.ns()
            create, calls = flaky(1, err)
            with self.assertRaises(RuntimeError):
                ns["_bg_llm_create"](client_with(create), {"branch_id": "bg-test"}, model="m")
            self.assertEqual(calls["n"], 1, err)

    def test_abandoned_no_retry(self):
        ns = self.ns()
        create, calls = flaky(1)
        with self.assertRaises(RuntimeError):
            ns["_bg_llm_create"](client_with(create), {"branch_id": "b", "abandoned": True}, model="m")
        self.assertEqual(calls["n"], 1)

    def test_inflight_marker_set_during_call(self):
        ns = self.ns()
        rc = {"branch_id": "bg-test"}
        seen = {}

        def create(**kw):
            seen["inflight"] = rc.get("llm_inflight_since")
            return "ok"
        ns["_bg_llm_create"](client_with(create), rc, model="m")
        self.assertIsNotNone(seen["inflight"])
        self.assertNotIn("llm_inflight_since", rc)


class DeadlineTest(unittest.TestCase):
    def run_check(self, rc, created_ago):
        branches = {"bg-x": SimpleNamespace(branch_id="bg-x", created_at=time.time() - created_ago,
                                            result_container=rc, request_ids={})}
        ns = load("check_background_deadline", _branch_lock=threading.Lock(),
                  _active_branches=branches, _merge_queue=queue.Queue())
        return ns["check_background_deadline"](), branches

    def test_inflight_call_not_abandoned(self):
        now = time.time()
        abandoned, branches = self.run_check({"llm_inflight_since": now - 400}, 450)
        self.assertFalse(abandoned)
        self.assertIn("bg-x", branches)

    def test_inflight_past_own_limit_abandoned(self):
        now = time.time()
        abandoned, branches = self.run_check({"llm_inflight_since": now - 1300}, 1350)
        self.assertTrue(abandoned)
        self.assertNotIn("bg-x", branches)

    def test_idle_without_call_abandoned(self):
        abandoned, branches = self.run_check({"last_progress_at": time.time() - 400}, 450)
        self.assertTrue(abandoned)

    def test_recent_progress_kept(self):
        abandoned, branches = self.run_check({"last_progress_at": time.time() - 10}, 450)
        self.assertFalse(abandoned)


if __name__ == "__main__":
    unittest.main()
