"""Background branch deadline resets on progress (idle deadline, R13 revised)."""
import ast
import json
import queue
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SRC = (HERE / "iter.py").read_text()
TREE = ast.parse(SRC)


def seg(name):
    node = next(n for n in TREE.body
                if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
    return ast.get_source_segment(SRC, node)


def deadline_ns(branch, deadline=300):
    ns = {"time": time, "_branch_lock": threading.Lock(), "_active_branches": {branch.branch_id: branch},
          "BACKGROUND_DEADLINE": deadline, "_merge_queue": queue.Queue()}
    exec(seg("check_background_deadline"), ns)
    return ns


def make_branch(age, last_progress_ago=None):
    ns = {"time": time}
    exec(seg("BranchState"), ns)
    rc = {"ok": True}
    b = ns["BranchState"]("bg-test", None, [], None, rc)
    now = time.time()
    b.created_at = now - age
    if last_progress_ago is not None:
        rc["last_progress_at"] = now - last_progress_ago
    return b


class IdleDeadlineTest(unittest.TestCase):
    def test_old_branch_with_recent_progress_is_kept(self):
        # The Sep 28 bg-448e8aff case: 301 s old, but still working.
        ns = deadline_ns(make_branch(age=900, last_progress_ago=10))
        self.assertFalse(ns["check_background_deadline"]())
        self.assertIn("bg-test", ns["_active_branches"])
        self.assertTrue(ns["_merge_queue"].empty())

    def test_idle_branch_is_abandoned(self):
        ns = deadline_ns(make_branch(age=900, last_progress_ago=301))
        self.assertTrue(ns["check_background_deadline"]())
        self.assertNotIn("bg-test", ns["_active_branches"])
        marker = ns["_merge_queue"].get_nowait()
        self.assertTrue(marker["abandoned"])
        self.assertIn("idle=", marker["content"])

    def test_no_progress_yet_falls_back_to_created_at(self):
        # Initial LLM call hung and never returned: still bounded.
        ns = deadline_ns(make_branch(age=301))
        self.assertTrue(ns["check_background_deadline"]())
        ns = deadline_ns(make_branch(age=100))
        self.assertFalse(ns["check_background_deadline"]())


class MiniLoopProgressTest(unittest.TestCase):
    def test_tool_results_and_llm_steps_refresh_progress(self):
        stamps = []

        class RC(dict):
            def __setitem__(self, k, v):
                if k == "last_progress_at":
                    stamps.append(v)
                super().__setitem__(k, v)

        def msg(tool):
            calls = None
            if tool:
                calls = [SimpleNamespace(id="c1", function=SimpleNamespace(name="noop", arguments="{}"))]
            return SimpleNamespace(tool_calls=calls,
                                   model_dump=lambda exclude_none=True: {"role": "assistant", "content": "x"})

        def resp(tool):
            return SimpleNamespace(choices=[SimpleNamespace(message=msg(tool))])

        follow = [resp(False)]
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: follow.pop(0))))
        ns = {"json": json, "time": time, "_merge_queue": queue.Queue(),
              "_branch_lock": threading.Lock(), "_active_branches": {},
              "_tool_context": threading.local(), "_release_branch": lambda *a: True,
              "load_tools": lambda: ({"noop": ("p",)}, [], []), "native_tools": lambda i: [],
              "invoke_dynamic": lambda *a, **k: {"ok": True, "result": "ok"},
              "MAX_TOOL_CALLS": 5, "MAX_TOOL_OUTPUT_CHARS": 1000, "get_current_time": lambda: "t",
              "KEEP_REASONING_IN_EPISODE": False, "BRANCH_STEP_BUDGET": 25, "MODEL": "m",
              "MAX_TOKENS": 10, "extract_tier1_checkpoint": lambda *a: {},
              "_llm_call_semaphore": threading.BoundedSemaphore(4), "LLM_TIMEOUT": 5,
              "ITER_MAX_CONCURRENT_LLM_CALLS": 4}
        exec(seg("_capped_llm_create"), ns)
        exec(seg("_bg_branch_mini_loop"), ns)
        ns["_bg_branch_mini_loop"]("bg-test", client, [], resp(True), RC())
        # one tool result + one follow-up LLM response
        self.assertEqual(len(stamps), 2)


if __name__ == "__main__":
    unittest.main()
