"""Multi-branch: ITER_MAX_CONCURRENT_LLM_CALLS parsing + semaphore cap.

iter.py runs its main loop at import time, so never `import iter` here;
extract the functions with ast and exec them in a small namespace.
"""
import ast
import os
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = (HERE / "iter.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def load(*names, **extra):
    segs = []
    for name in names:
        node = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
        segs.append(ast.get_source_segment(SRC, node))
    ns = {"os": os, "threading": threading, "ITER_MAX_BACKGROUND_BRANCHES": 3,
          "_tracked_llm_create": lambda c, **kw: c.chat.completions.create(**kw),  # model tag: plain create
          "LLM_TIMEOUT": 1, "ITER_MAX_CONCURRENT_LLM_CALLS": 0, **extra}
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


class ParseTest(unittest.TestCase):
    def test_default_invalid_and_clamp(self):
        f = load("_max_concurrent_llm_calls")["_max_concurrent_llm_calls"]
        old = os.environ.pop("ITER_MAX_CONCURRENT_LLM_CALLS", None)
        try:
            self.assertEqual(f(), 4)
        finally:
            if old is not None:
                os.environ["ITER_MAX_CONCURRENT_LLM_CALLS"] = old
        self.assertEqual([f(""), f("abc"), f("0"), f("-2"), f("1"), f("3"), f("999")],
                         [4, 4, 1, 1, 1, 3, 4])


class CapTest(unittest.TestCase):
    def test_cap_limits_parallel_calls(self):
        ns = load("_capped_llm_create", _llm_call_semaphore=threading.BoundedSemaphore(2),
                  LLM_TIMEOUT=5)
        state = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def create(**kw):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return kw["model"]

        client = client_with(create)
        out, out_lock = [], threading.Lock()

        def worker():
            r = ns["_capped_llm_create"](client, model="m")
            with out_lock:
                out.append(r)

        ts = [threading.Thread(target=worker) for _ in range(6)]
        [t.start() for t in ts]
        [t.join(5) for t in ts]
        self.assertEqual(out, ["m"] * 6)
        self.assertEqual(state["peak"], 2)

    def test_slot_released_on_error(self):
        sem = threading.BoundedSemaphore(1)
        ns = load("_capped_llm_create", _llm_call_semaphore=sem)

        def create(**kw):
            raise RuntimeError("boom")

        client = client_with(create)
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                ns["_capped_llm_create"](client, model="m")
        self.assertTrue(sem.acquire(timeout=0.1))

    def test_no_slot_times_out(self):
        sem = threading.BoundedSemaphore(1)
        sem.acquire()
        ns = load("_capped_llm_create", _llm_call_semaphore=sem, LLM_TIMEOUT=0.2)
        client = client_with(lambda **kw: "never")
        t0 = time.monotonic()
        with self.assertRaises(Exception) as cm:
            ns["_capped_llm_create"](client, model="m")
        self.assertIn("concurrency cap", str(cm.exception))
        self.assertLess(time.monotonic() - t0, 2)


if __name__ == "__main__":
    unittest.main()
