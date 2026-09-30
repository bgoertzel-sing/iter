"""Multi-branch Iter: capped branch set, request binding, per-branch clean-up."""
import ast
import hashlib
import importlib.util
import json
import os
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SRC = (HERE / "iter.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
CHANNEL_WT = HERE.parent / "wt-multibranch-channel"


def seg(*names):
    out = []
    for name in names:
        node = next(n for n in TREE.body
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
        out.append(ast.get_source_segment(SRC, node))
    return "\n\n".join(out)


def load(*names, **extra):
    ns = {"time": time, "threading": threading, "json": json, "os": os, "Path": Path,
          "queue": queue, "_branch_lock": threading.Lock(), "_active_branches": {},
          "_merge_queue": queue.Queue(), "_foreground_request_ids": {},
          "_tool_context": threading.local(), "ITER_REQUEST_BINDING": True, **extra}
    exec(seg(*names), ns)
    return ns


def drain(q):
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


class ConfigTest(unittest.TestCase):
    def test_cap_default_clamp_and_invalid(self):
        ns = load("_max_background_branches", MAX_BACKGROUND_BRANCHES_LIMIT=8)
        f = ns["_max_background_branches"]
        old = os.environ.pop("ITER_MAX_BACKGROUND_BRANCHES", None)
        try:
            self.assertEqual(f(), 3)
        finally:
            if old is not None:
                os.environ["ITER_MAX_BACKGROUND_BRANCHES"] = old
        self.assertEqual([f("1"), f("0"), f("-5"), f("99"), f("5"), f("x")], [1, 1, 1, 8, 5, 1])


def branch_ns(**extra):
    ns = load("BranchState", "_release_branch", "check_background_deadline",
              BACKGROUND_DEADLINE=300, **extra)
    return ns


def add_branch(ns, bid, idle, rids=None):
    b = ns["BranchState"](bid, None, [], None, {"ok": True}, request_ids=rids)
    now = time.time()
    b.created_at = now - 1000
    b.result_container["last_progress_at"] = now - idle
    ns["_active_branches"][bid] = b
    return b


class DeadlineManyTest(unittest.TestCase):
    def test_only_idle_branches_are_abandoned(self):
        ns = branch_ns()
        add_branch(ns, "bg-a", idle=400, rids={"pc": "a" * 64})
        add_branch(ns, "bg-b", idle=10, rids={"pc": "b" * 64})
        add_branch(ns, "bg-c", idle=301)
        self.assertTrue(ns["check_background_deadline"]())
        self.assertEqual(sorted(ns["_active_branches"]), ["bg-b"])
        items = drain(ns["_merge_queue"])
        abandoned = sorted(i["branch"] for i in items if i.get("abandoned"))
        done = [i for i in items if i.get("_branch_done")]
        self.assertEqual(abandoned, ["bg-a", "bg-c"])
        self.assertEqual([(d["branch"], d["request_ids"]) for d in done], [("bg-a", {"pc": "a" * 64})])
        self.assertFalse(ns["check_background_deadline"]())


class ReleaseTest(unittest.TestCase):
    def test_release_pops_once_and_hands_back_request(self):
        ns = branch_ns()
        b = add_branch(ns, "bg-x", idle=0, rids={"pc": "c" * 64})
        self.assertTrue(ns["_release_branch"]("bg-x", b.result_container, "complete"))
        self.assertTrue(b.result_container["finished"])
        self.assertFalse(ns["_release_branch"]("bg-x", b.result_container, "complete"))
        items = drain(ns["_merge_queue"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason"], "complete")


class FakeOpenAI:
    def __init__(self, **kw):
        self.kw = kw


def promotion_ns(cap, active, delay=0.3):
    started = []
    ns = load("BranchState", "_release_branch", "background_branch_active",
              "background_branch_count", "record_branch_error", "_bg_llm_thread_target",
              "threaded_llm_call",
              ITER_PROMOTE_SECONDS=0.05, ITER_MAX_BACKGROUND_BRANCHES=cap, LLM_TIMEOUT=5,
              uuid=__import__("uuid"), copy=__import__("copy"),
              openai=SimpleNamespace(OpenAI=FakeOpenAI), API_KEY="k", BASE_URL="u", SESSION_ID="s",
              _branch_error=None,
              _bg_branch_mini_loop=lambda bid, c, m, r, rc: started.append((bid, rc.get("request_ids"))))
    for i in range(active):
        ns["_active_branches"][f"bg-old{i}"] = ns["BranchState"](f"bg-old{i}", None, [], None, {})

    def create(**kw):
        time.sleep(delay)
        return "RESPONSE"
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return ns, client, started


class PromotionTest(unittest.TestCase):
    def test_promotes_while_below_cap_and_binds_foreground_request(self):
        ns, client, started = promotion_ns(cap=3, active=2)
        ns["_foreground_request_ids"]["pc"] = "d" * 64
        response, branch = ns["threaded_llm_call"](client, "m", [], [], "auto", 1, {})
        self.assertIsNone(response)
        self.assertEqual(branch.request_ids, {"pc": "d" * 64})
        self.assertEqual(len(ns["_active_branches"]), 3)
        self.assertEqual(ns["_foreground_request_ids"], {})  # branch owns it now
        branch.thread.join(2)
        self.assertEqual(started, [(branch.branch_id, {"pc": "d" * 64})])

    def test_full_set_answers_in_foreground_without_evicting(self):
        ns, client, started = promotion_ns(cap=2, active=2)
        response, branch = ns["threaded_llm_call"](client, "m", [], [], "auto", 1, {})
        self.assertEqual((response, branch), ("RESPONSE", None))
        self.assertEqual(sorted(ns["_active_branches"]), ["bg-old0", "bg-old1"])
        self.assertEqual(started, [])


def write_fake_channel(root, name="pc"):
    (root / "channels").mkdir(exist_ok=True)
    (root / "channels" / (name + ".py")).write_text(
        "import json, os\n"
        "LOG = os.path.join(os.path.dirname(__file__), 'sent.jsonl')\n"
        "def receive_request():\n    return None\n"
        "def close_active(note=None, request_id=None):\n    return False\n"
        "def send(content, final=True, request_id=None):\n"
        "    with open(LOG, 'a') as f:\n"
        "        f.write(json.dumps([content, final, request_id]) + '\\n')\n")
    return root / "channels" / "sent.jsonl"


class SendBindingTest(unittest.TestCase):
    def run_send(self, env_ids, **kw):
        spec = importlib.util.spec_from_file_location("send_tool", HERE / "tools" / "send.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with TemporaryDirectory() as tmp:
            log = write_fake_channel(Path(tmp))
            cwd = os.getcwd()
            old = os.environ.pop("ITER_REQUEST_IDS", None)
            try:
                os.chdir(tmp)
                if env_ids is not None:
                    os.environ["ITER_REQUEST_IDS"] = json.dumps(env_ids)
                result = mod.run("pc", "hi", **kw)
            finally:
                os.chdir(cwd)
                os.environ.pop("ITER_REQUEST_IDS", None)
                if old is not None:
                    os.environ["ITER_REQUEST_IDS"] = old
            sent = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        return result, sent

    def test_bound_send_answers_its_own_request(self):
        result, sent = self.run_send({"pc": "e" * 64})
        self.assertEqual(result, "SUCCESS")
        self.assertEqual(sent, [["hi", True, "e" * 64]])
        result, sent = self.run_send({"pc": "e" * 64}, final="false")
        self.assertEqual(sent, [["hi", False, "e" * 64]])

    def test_unbound_turn_is_refused_in_binding_mode(self):
        result, sent = self.run_send({})
        self.assertIn("No open pc request is bound", result)
        self.assertEqual(sent, [])

    def test_legacy_mode_unchanged(self):
        result, sent = self.run_send(None)
        self.assertEqual(result, "SUCCESS")
        self.assertEqual(sent, [["hi", True, None]])


class ReceiveBindingTest(unittest.TestCase):
    def test_foreground_claims_one_request_at_a_time(self):
        with TemporaryDirectory() as tmp:
            write_fake_channel(Path(tmp))
            state = {"inbox": ["1" * 64, "2" * 64], "open": []}
            calls = []

            def invoke(path, function, *a, **k):
                calls.append(function)
                if function == "active_request_ids":
                    return {"ok": True, "result": list(state["open"])}
                if function == "receive_request":
                    if not state["inbox"]:
                        return {"ok": True, "result": None}
                    rid = state["inbox"].pop(0)
                    state["open"].append(rid)
                    return {"ok": True, "result": {"request_id": rid, "prompt": "p" + rid[0]}}
                raise AssertionError(function)
            ns = load("_channel_has", "_foreground_busy", "receive", invoke_dynamic=invoke)
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertEqual(ns["receive"](), "[pc] p1")
                self.assertEqual(ns["_foreground_request_ids"], {"pc": "1" * 64})
                self.assertEqual(ns["receive"](), "")          # request 1 still open
                state["open"].remove("1" * 64)                  # answered
                self.assertEqual(ns["receive"](), "[pc] p2")
                self.assertEqual(ns["_foreground_request_ids"], {"pc": "2" * 64})
                ns["_foreground_request_ids"].clear()           # promoted: foreground free
                self.assertEqual(ns["receive"](), "")          # inbox empty
            finally:
                os.chdir(cwd)


class CloseTest(unittest.TestCase):
    def test_turn_end_closes_only_foreground_request(self):
        with TemporaryDirectory() as tmp:
            write_fake_channel(Path(tmp))
            calls = []

            def invoke(path, function, *a, **k):
                calls.append((function, k))
                return {"ok": True, "result": function == "close_active"}
            ns = load("background_branch_active", "close_unfinished_requests",
                      invoke_dynamic=invoke, ITER_CONCURRENCY_ENABLED=True, ITER_REQUEST_TIMEOUT=0)
            ns["_active_branches"]["bg-1"] = object()
            ns["_foreground_request_ids"]["pc"] = "f" * 64
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                ns["close_unfinished_requests"]()
                ns["close_unfinished_requests"]()  # nothing left to close
            finally:
                os.chdir(cwd)
            self.assertEqual(calls, [("close_active", {"request_id": "f" * 64})])

    def test_branch_done_skips_request_the_foreground_owns(self):
        calls = []
        ns = load("_close_branch_requests",
                  invoke_dynamic=lambda p, f, *a, **k: calls.append(k) or {"ok": True, "result": True})
        ns["_foreground_request_ids"]["pc"] = "a" * 64
        ns["_close_branch_requests"]({"branch": "bg", "request_ids": {"pc": "a" * 64, "tg": "b" * 64}})
        self.assertEqual(calls, [{"request_id": "b" * 64}])


@unittest.skipUnless((CHANNEL_WT / "src" / "iter_outer_channel_protocosmo2.py").is_file(),
                     "channel worktree not present")
class RealChannelTest(unittest.TestCase):
    """End to end: real channel contract + tools/send.py, three overlapping requests."""

    def test_three_open_requests_each_answered_by_its_binding(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            root = tmp / "chan"
            (tmp / "channels").mkdir()
            (tmp / "channels" / "pc.py").write_text(
                f"import sys\nsys.path.insert(0, {str(CHANNEL_WT)!r})\n"
                "from src.iter_outer_channel_protocosmo2 import send, receive_request, active_request_ids\n")
            env = {"ITER_OUTER_CHANNEL_ROOT": str(root), "ITER_MAX_OPEN_REQUESTS": "4",
                   "ITER_OUTER_IDENTITY": "ProtoCosmo2"}
            saved = {k: os.environ.get(k) for k in list(env) + ["ITER_REQUEST_IDS"]}
            os.environ.update(env)
            sys.path.insert(0, str(CHANNEL_WT))
            try:
                spec = importlib.util.spec_from_file_location("chan", CHANNEL_WT / "src" / "iter_outer_channel_protocosmo2.py")
                chan = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(chan)
                chan._root()
                ids = []
                for n in range(3):
                    prompt = f"question {n}"
                    rid = hashlib.sha256(prompt.encode()).hexdigest()
                    ids.append(rid)
                    req = {"schema_version": 1, "identity": "ProtoCosmo2", "request_id": rid,
                           "prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                           "source": {"chat_id": 1, "message_id": 100 + n, "update_id": 200 + n}}
                    (root / "inbox" / (rid + ".json")).write_text(json.dumps(req))
                claimed = [chan.receive_request()["request_id"] for _ in range(3)]
                self.assertEqual(sorted(claimed), sorted(ids))
                spec2 = importlib.util.spec_from_file_location("send_tool2", HERE / "tools" / "send.py")
                send_tool = importlib.util.module_from_spec(spec2)
                spec2.loader.exec_module(send_tool)
                cwd = os.getcwd()
                os.chdir(tmp)
                try:
                    for rid in reversed(claimed):  # answer out of order
                        os.environ["ITER_REQUEST_IDS"] = json.dumps({"pc": rid})
                        self.assertEqual(send_tool.run("pc", "answer " + rid[:6]), "SUCCESS")
                finally:
                    os.chdir(cwd)
                for rid in claimed:
                    out = json.loads((root / "outbox" / (rid + ".json")).read_text())
                    self.assertEqual(out["response"], "answer " + rid[:6])
                self.assertEqual(chan.active_request_ids(), [])
            finally:
                sys.path.remove(str(CHANNEL_WT))
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
