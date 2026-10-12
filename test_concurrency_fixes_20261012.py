"""2026-10-12 concurrency fixes: promotion race, original provider error (K9),
binding at 1 branch (D7), send_document with several open requests."""
import ast
import importlib.util
import json
import os
import threading
import time
import unittest
import uuid as real_uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import test_multi_branch as mb

HERE = Path(__file__).resolve().parent


def gated_client(gate, result="RESPONSE", error=None):
    def create(**kw):
        gate.wait(5)
        if error is not None:
            raise error
        return result
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class PromotionRaceTest(unittest.TestCase):
    def test_call_finishing_during_setup_still_runs_branch_loop(self):
        """Old code published branch_id before branch_client/branch_messages, so a call
        that returned in that window skipped the mini-loop and its answer was lost."""
        ns, _, started = mb.promotion_ns(cap=3, active=0)
        gate = threading.Event()
        real_copy = ns["copy"]

        def deepcopy(value):  # runs inside the promotion setup
            gate.set()
            time.sleep(0.2)   # let the model call return now
            return real_copy.deepcopy(value)
        ns["copy"] = SimpleNamespace(deepcopy=deepcopy)
        response, branch = ns["threaded_llm_call"](gated_client(gate), "m", [], [], "auto", 1, {})
        self.assertIsNone(response)
        branch.thread.join(2)
        self.assertEqual([bid for bid, _ in started], [branch.branch_id])

    def test_call_finishing_at_the_deadline_is_answered_in_foreground(self):
        ns, _, started = mb.promotion_ns(cap=3, active=0)
        gate = threading.Event()

        def uuid4():  # runs after is_alive() said "promote", before the promotion lock
            gate.set()
            time.sleep(0.2)
            return real_uuid.uuid4()
        ns["uuid"] = SimpleNamespace(uuid4=uuid4)
        ns["_foreground_request_ids"]["pc"] = "e" * 64
        response, branch = ns["threaded_llm_call"](gated_client(gate), "m", [], [], "auto", 1, {})
        self.assertEqual((response, branch), ("RESPONSE", None))
        self.assertEqual(ns["_active_branches"], {})
        self.assertEqual(ns["_foreground_request_ids"], {"pc": "e" * 64})  # still the foreground's
        self.assertEqual(started, [])


class ProviderError(Exception):
    status_code = 429
    response = SimpleNamespace(headers={"retry-after": "7"})


class OriginalErrorTest(unittest.TestCase):
    def test_failed_call_reraises_the_original_exception(self):
        ns, _, _ = mb.promotion_ns(cap=3, active=0)
        ns["ITER_PROMOTE_SECONDS"] = 2
        error = ProviderError("Error code: 429")
        gate = threading.Event()
        gate.set()
        with self.assertRaises(ProviderError) as caught:
            ns["threaded_llm_call"](gated_client(gate, error=error), "m", [], [], "auto", 1, {})
        self.assertIs(caught.exception, error)
        self.assertEqual(caught.exception.response.headers["retry-after"], "7")


class BindingTest(unittest.TestCase):
    def binding(self, concurrency, branches):
        tree = ast.parse((HERE / "iter.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", None) == "ITER_REQUEST_BINDING" for t in n.targets))
        return eval(compile(ast.Expression(node.value), "iter.py", "eval"),
                    {"ITER_CONCURRENCY_ENABLED": concurrency, "ITER_MAX_BACKGROUND_BRANCHES": branches})

    def test_binding_follows_concurrency_even_with_one_branch(self):
        self.assertTrue(self.binding(True, 1))
        self.assertTrue(self.binding(True, 3))
        self.assertFalse(self.binding(False, 3))


@unittest.skipUnless((HERE / "tools" / "send_document.py").exists(), "tools/send_document.py not in this checkout")
class SendDocumentSourceTest(unittest.TestCase):
    def source(self, open_requests, env_ids):
        spec = importlib.util.spec_from_file_location("send_document_tool", HERE / "tools" / "send_document.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        saved = {k: os.environ.get(k) for k in ("ITER_OUTER_CHANNEL_ROOT", "ITER_REQUEST_IDS")}
        with TemporaryDirectory() as root:
            (Path(root) / "processing").mkdir()
            for rid, chat in open_requests.items():
                (Path(root) / "processing" / (rid + ".json")).write_text(
                    json.dumps({"request_id": rid, "source": {"chat_id": chat}}))
            os.environ["ITER_OUTER_CHANNEL_ROOT"] = root
            os.environ.pop("ITER_REQUEST_IDS", None)
            if env_ids is not None:
                os.environ["ITER_REQUEST_IDS"] = json.dumps(env_ids)
            try:
                return mod._source()
            finally:
                for k, v in saved.items():
                    os.environ.pop(k, None)
                    if v is not None:
                        os.environ[k] = v

    def test_bound_turn_uses_its_own_request_among_several(self):
        reqs = {"a" * 64: 111, "b" * 64: 222}
        self.assertEqual(self.source(reqs, {"protocosmo2": "b" * 64}), 222)

    def test_bound_turn_without_a_request_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.source({"a" * 64: 111}, {"protocosmo2": None})

    def test_bound_request_already_closed_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.source({"a" * 64: 111}, {"protocosmo2": "c" * 64})

    def test_unbound_legacy_single_request_still_works(self):
        self.assertEqual(self.source({"a" * 64: 111}, None), 111)
        with self.assertRaises(RuntimeError):
            self.source({"a" * 64: 111, "b" * 64: 222}, None)


if __name__ == "__main__":
    unittest.main()
