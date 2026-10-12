"""PROGRESS_NOT_PAUSE (2026-10-04): interim progress notes instead of the step-budget stop.

Port of the VM2 change (experiments/20261003T1600Z-iter-progress-not-pause) to ProtoCosmo2.
- StepGuard unit tests (fast, ast-extracted; iter.py runs its main loop at import).
- Real main-loop scenarios via progress_harness.py (fake OpenAI client, real channel/tools),
  foreground and promoted background branches.  HARNESS_ITER_PY=<old iter.py> reruns the
  scenarios on old code as a negative control.
- Channel/responder/receiver: notes are delivered as Telegram replies and keep long work alive.
"""
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from progress_guard_testlib import add_step_guard

HERE = Path(__file__).resolve().parent
SRC = (HERE / "iter.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
RUNNER = HERE.parents[1] / "tools" / "phase6_private_canary_runner.py"
sys.path.insert(0, str(HERE.parent))


def seg(*names, source=SRC, tree=TREE):
    out = []
    for name in names:
        node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
        out.append(ast.get_source_segment(source, node))
    return "\n\n".join(out)


def call(name, arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def guard(**overrides):
    ns = add_step_guard({}, **overrides)
    return ns, ns["StepGuard"]()


def step(g, i, name="work", ret=None, args=None):
    args = args if args is not None else {"arg": f"x{i}"}
    return g.record_step([call(name, args)], [(name, json.dumps(args), ret if ret is not None else f"r{i}")])


class StepGuardTest(unittest.TestCase):
    def test_nudge_at_30_fallback_at_32_with_model_free_text(self):
        _, g = guard()
        actions = {i: step(g, i) for i in range(1, 40)}
        self.assertEqual(actions[30][0], "nudge")
        self.assertIn("call send with final=false", actions[30][1])
        self.assertIn("KEEP WORKING", actions[30][1])
        self.assertEqual(actions[32][0], "fallback")
        self.assertIn("Still working on this (step 32), not paused", actions[32][1])
        self.assertIn("work x32", actions[32][1])
        self.assertEqual([i for i, a in actions.items() if a[0]], [30, 32])

    def test_model_progress_note_suppresses_fallback(self):
        _, g = guard()
        for i in range(1, 31):
            step(g, i)
        self.assertEqual(step(g, 31, "send", "SUCCESS (progress; request still open)",
                              {"channel": "protocosmo2", "content": "note", "final": False}), (None, None))
        self.assertFalse(g.answered)
        self.assertEqual([step(g, i)[0] for i in range(32, 40)], [None] * 8)

    def test_final_send_stops_interim_notes(self):
        _, g = guard()
        step(g, 1, "send", "SUCCESS", {"channel": "protocosmo2", "content": "answer"})
        self.assertTrue(g.answered)
        self.assertEqual({step(g, i)[0] for i in range(2, 70)}, {None})

    def test_no_request_no_notes(self):
        ns = add_step_guard({})
        g = ns["StepGuard"](has_request=False)
        self.assertEqual({step(g, i)[0] for i in range(1, 70)}, {None})

    def test_silence_timer_nudges(self):
        clock = [1000.0]
        ns, _ = guard(time=SimpleNamespace(time=lambda: clock[0]))
        g = ns["StepGuard"]()
        self.assertEqual(step(g, 1), (None, None))
        clock[0] += 421
        self.assertEqual(step(g, 2)[0], "nudge")
        self.assertEqual(step(g, 3)[0], None)
        self.assertEqual(step(g, 4)[0], "fallback")

    def test_backstops(self):
        _, g = guard(HARD_STEP_CEILING=12)
        results = [step(g, i) for i in range(1, 13)]
        self.assertEqual(results[-1][0], "stop")
        self.assertIn("step ceiling (12 steps)", results[-1][1])
        _, g = guard()
        results = [step(g, i, args={"arg": "same"}, ret="same") for i in range(1, 9)]
        self.assertEqual(results[-1][0], "stop")
        self.assertIn("last 8 steps repeated the same tool call", results[-1][1])
        self.assertEqual({r[0] for r in results[:-1]}, {None})
        _, g = guard()
        results = [step(g, i, args={"arg": "ab"[i % 2]}, ret="ab"[i % 2]) for i in range(1, 15)]
        self.assertEqual(results[13][0], "stop")  # 2 new steps, then 12 stale ones
        self.assertIn("last 12 steps produced no new results", results[13][1])

    def test_distinct_work_runs_to_ceiling_default(self):
        _, g = guard()
        stops = [i for i in range(1, 301) if step(g, i)[0] == "stop"]
        self.assertEqual(stops, [300])

    def test_fork_keeps_request_count(self):
        _, g = guard()
        for i in range(1, 29):
            step(g, i)
        b = g.fork()
        self.assertEqual(b.steps, 28)
        step(b, 29)
        self.assertEqual(step(b, 30)[0], "nudge")
        self.assertEqual(g.steps, 28)

    def test_env_names_and_defaults(self):
        for name, default in (("ITER_PROGRESS_EVERY", 30), ("ITER_PROGRESS_FALLBACK_STEPS", 2),
                              ("ITER_PROGRESS_MAX_SILENT_SECONDS", 420),
                              ("ITER_HARD_STEP_CEILING", 300), ("ITER_REPEAT_STEPS", 8), ("ITER_STALL_STEPS", 12)):
            self.assertIn(f'_env_int("{name}", {default}', SRC)
        self.assertIn("BRANCH_STEP_BUDGET = HARD_STEP_CEILING", SRC)
        self.assertNotIn("TURN BUDGET WARNING", SRC)


class SendProgressNoteTest(unittest.TestCase):
    def test_non_closing_send(self):  # REFERENCE BRANCH: model tag excluded (live: test_non_closing_send_with_model_tag)
        calls = []
        ns = {"Path": Path, "CHECKPOINT_CHANNEL": "protocosmo2",
              "invoke_dynamic": lambda path, fn, **kw: calls.append((str(path), fn, kw)) or
              {"ok": True, "result": "SUCCESS (progress; request still open)"}}
        exec(seg("send_progress_note"), ns)
        self.assertTrue(ns["send_progress_note"]("Still working on this (step 32)"))
        self.assertEqual(calls, [("tools/send.py", "run", {"channel": "protocosmo2", "final": False,
                                  "content": "Still working on this (step 32)"})])

    def test_refused_note_reported(self):
        ns = {"Path": Path, "CHECKPOINT_CHANNEL": "protocosmo2",
              "invoke_dynamic": lambda *a, **k: {"ok": True, "result": "No open protocosmo2 request is bound"}}
        exec(seg("send_progress_note"), ns)  # REFERENCE BRANCH: model tag excluded
        self.assertFalse(ns["send_progress_note"]("x"))

    def test_stop_message_states_reason(self):
        ns = {}
        exec(seg("format_checkpoint_message"), ns)
        text = ns["format_checkpoint_message"]({"step_count": 300, "stop_reason": "it reached the per-request step ceiling (300 steps)",
                                                "tool_snapshot": []}, None)
        self.assertIn("[PAUSED] I stopped working on this at step 300 because it reached the per-request step ceiling", text)
        self.assertIn("reply 'continue'", text)


class BranchMiniLoopTest(unittest.TestCase):
    """Background branch: own step count, interim notes with the model tag, no 25-step stop."""

    def run_branch(self, n_steps, guard_overrides=None, final_at=None):
        import queue
        sent, notes = [], []

        def resp(i):
            if final_at and i > final_at:
                calls = None
            elif final_at and i == final_at:
                calls = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(
                    name="send", arguments=json.dumps({"channel": "protocosmo2", "content": "done"})))]
            elif i > n_steps:
                calls = None
            else:
                calls = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name="work", arguments=json.dumps({"arg": f"x{i}"})))]
            msg = SimpleNamespace(tool_calls=calls, content="x",
                                  model_dump=lambda exclude_none=True: {"role": "assistant", "content": "x"})
            return SimpleNamespace(id=f"chatcmpl_{i}", choices=[SimpleNamespace(message=msg)])

        counter = [1]

        def create(client, rc, **kw):
            counter[0] += 1
            return resp(counter[0])

        q = queue.Queue()
        ns = {"json": json, "time": time, "_merge_queue": q, "_mark_branch_activity_done": lambda b: None,
              "take_branch_inbox": lambda b: "", "sibling_view": lambda b: "", "_note_send": lambda o, c: None,
              "_tool_context": threading.local(), "_release_branch": lambda *a: True,
              "load_tools": lambda: ({"work": ("w",), "send": ("s",)}, [], []), "native_tools": lambda i: [],
              "invoke_dynamic": lambda path, fn, **kw: (sent.append(kw) or {"ok": True, "result": "SUCCESS"}) if path == "s"
              else {"ok": True, "result": "r:" + kw.get("arg", "")},
              "MAX_TOOL_CALLS": 5, "MAX_TOOL_OUTPUT_CHARS": 1000, "get_current_time": lambda: "t",
              "KEEP_REASONING_IN_EPISODE": False, "BRANCH_STEP_BUDGET": 300, "MODEL": "m", "MAX_TOKENS": 10,
              "extract_tier1_checkpoint": lambda msgs, n: {"step_count": n, "tool_snapshot": []},
              "_bg_llm_create": create, "record_branch_error": lambda e: None,
              "tag_send_arguments": lambda name, args, response: args,
              "send_text_only_fallback": lambda content, tag="": True,
              "served_model_tag": lambda r: "[claude-opus-4-6]"}
        add_step_guard(ns, **(guard_overrides or {}))
        ns["send_progress_note"] = lambda content, tag="": notes.append((tag, content)) or True
        exec(seg("_bg_branch_mini_loop"), ns)
        rc = {"request_ids": {"protocosmo2": "a" * 64}}
        ns["_bg_branch_mini_loop"]("bg-t", None, [], resp(1), rc)
        payloads = []
        while not q.empty():
            item = q.get()
            if item.get("_checkpoint_payload"):
                payloads.append(item)
        return notes, payloads, sent, ns

    def test_branch_runs_past_old_budget_with_interim_notes(self):
        notes, payloads, sent, _ = self.run_branch(60, final_at=61)
        self.assertEqual([n[1].split(")")[0] for n in notes], ["Still working on this (step 32"])
        self.assertEqual(notes[0][0], "")  # REFERENCE BRANCH: model tag excluded (live: "[claude-opus-4-6]")
        self.assertEqual(payloads, [])  # no checkpoint/stop at 25
        self.assertEqual([s["content"] for s in sent], ["done"])

    def test_branch_ceiling_queues_stop_with_reason(self):
        notes, payloads, sent, ns = self.run_branch(500, guard_overrides={"HARD_STEP_CEILING": 40})
        self.assertEqual(len(payloads), 1)
        self.assertIn("step ceiling (40 steps)", payloads[0]["stop_reason"])
        self.assertEqual(payloads[0]["step_count"], 40)
        self.assertTrue(ns["StepGuard"].paused_by_backstop)

    def test_branch_inherits_forked_request_count(self):
        ns = add_step_guard({})
        g = ns["StepGuard"]()
        for i in range(1, 21):
            g.record_step([call("work", {"arg": f"p{i}"})], [("work", "{}", f"p{i}")])
        self.assertIn('result_container["step_guard"] = fg_guard.fork()', SRC)
        self.assertLess(SRC.index('result_container["step_guard"] = fg_guard.fork()'),
                        SRC.index('result_container["branch_id"] = branch_id  # R14'))


def run_scenario(scenario, iter_py=None, timeout=300):
    tmp = Path(tempfile.mkdtemp(prefix="pc2-pnp-"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("ITER_")}
    if iter_py or os.environ.get("HARNESS_ITER_PY"):
        env["HARNESS_ITER_PY"] = str(iter_py or os.environ["HARNESS_ITER_PY"])
    proc = subprocess.run([sys.executable, str(HERE / "progress_harness.py"), str(tmp / "w"), json.dumps(scenario)],
                          env=env, capture_output=True, text=True, timeout=timeout)
    log = proc.stdout + proc.stderr
    summary_path = tmp / "w" / "summary.json"
    if proc.returncode != 0 or not summary_path.exists():
        raise AssertionError("harness failed:\n" + log[-3000:])
    summary = json.loads(summary_path.read_text())
    calls = [json.loads(line) for line in (tmp / "w" / "calls.jsonl").read_text().splitlines()]
    shutil.rmtree(tmp, ignore_errors=True)
    return summary, calls, log


BRANCHES = {"ITER_CONCURRENCY_ENABLED": "1", "ITER_PROMOTE_SECONDS": "1", "ITER_MAX_BACKGROUND_BRANCHES": "3"}


class MainLoopScenarios(unittest.TestCase):
    """The real iter.py main loop; each scenario ~10-40 s."""

    def test_foreground_interval_fallback_and_work_continues(self):
        s, calls, log = run_scenario({"policy": "finish_at", "finish": 18, "events": [{"text": "long task"}],
                                      "env": {"ITER_PROGRESS_EVERY": "5", "ITER_HARD_STEP_CEILING": "40"}})
        notes = list(s["progress"].values())
        self.assertEqual(len(notes), 3, notes)  # nudges at 5/10/15, fallbacks at 7/12/17
        self.assertTrue(all("Still working on this" in v and "not paused" in v for v in notes))
        self.assertIn("step 7", notes[0])
        self.assertEqual(list(s["outbox"].values()), ["final"])  # request stayed open until the real reply
        self.assertNotIn("BACKSTOP", log)
        self.assertNotIn("Turn budget exhausted", log)
        self.assertEqual(s["calls"], 19)  # 18 task steps + the single throttled post-task turn

    def test_model_posts_progress_so_no_fallback(self):
        s, calls, log = run_scenario({"policy": "work_obey_progress", "events": [{"text": "long task"}],
                                      "env": {"ITER_PROGRESS_EVERY": "5", "ITER_HARD_STEP_CEILING": "14"}})
        notes = list(s["progress"].values())
        self.assertTrue(any("model note at call 6" in v for v in notes), notes)
        self.assertFalse(any("Still working on this" in v for v in notes), notes)
        self.assertNotIn("PROGRESS FALLBACK", log)

    def test_hard_ceiling_stops_with_reason_then_provider_silent(self):
        s, calls, log = run_scenario({"policy": "work_forever", "events": [{"text": "long task"}], "idle_limit": 80,
                                      "env": {"ITER_PROGRESS_EVERY": "5", "ITER_HARD_STEP_CEILING": "12"}})
        self.assertEqual(s["calls"], 12, log[-1500:])
        notice = " ".join(s["outbox"].values())
        self.assertIn("stopped working on this at step 12 because it reached the per-request step ceiling (12 steps)", notice)
        self.assertIn("reply 'continue'", notice)
        self.assertEqual(s["stop"], "idle")
        self.assertEqual(s["processing"], {})

    def test_repeated_identical_steps_backstop(self):
        s, calls, log = run_scenario({"policy": "repeat_same", "events": [{"text": "t"}],
                                      "env": {"ITER_REPEAT_STEPS": "4", "ITER_STALL_STEPS": "50", "ITER_PROGRESS_EVERY": "100"}})
        self.assertEqual(s["calls"], 4)
        self.assertIn("repeated the same tool call", " ".join(s["outbox"].values()))

    def test_no_new_results_backstop(self):
        s, calls, log = run_scenario({"policy": "cycle_two", "events": [{"text": "t"}],
                                      "env": {"ITER_REPEAT_STEPS": "50", "ITER_STALL_STEPS": "6", "ITER_PROGRESS_EVERY": "100"}})
        self.assertEqual(s["calls"], 8)
        self.assertIn("produced no new results", " ".join(s["outbox"].values()))

    def test_defaults_no_stop_note_at_32_request_open(self):
        s, calls, log = run_scenario({"policy": "work_forever", "events": [{"text": "t"}], "max_calls": 33})
        self.assertEqual(s["stop"], "max_calls")
        self.assertNotIn("BACKSTOP", log)
        self.assertEqual(s["outbox"], {})
        notes = list(s["progress"].values())
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("step 32", notes[0])  # default cadence 30 + fallback 2
        self.assertEqual(len(s["processing"]), 1)  # still open, not closed by progress
        self.assertTrue(any("PROGRESS CHECKPOINT: you are at step 30" in " ".join(c["recent_user"]) for c in calls))

    def test_idle_without_event_is_provider_silent(self):
        # Existing behaviour (idle-loop fix 2026-09-27): one throttled startup check-in, then silence.
        s, calls, log = run_scenario({"policy": "work_forever", "events": [], "idle_limit": 120})
        self.assertEqual(s["calls"], 1)
        self.assertIn("TASK COMPLETED", " ".join(calls[0]["recent_user"]))

    def test_after_nop_only_the_throttled_post_task_turn(self):
        s, calls, log = run_scenario({"policy": "reply_nop", "events": [{"text": "hi"}], "idle_limit": 60})
        self.assertEqual(s["calls"], 2)
        self.assertEqual(list(s["outbox"].values()), ["hi"])

    def test_resume_note_after_backstop(self):
        s, calls, log = run_scenario({"policy": "repeat_same",
                                      "events": [{"text": "t"}, {"text": "continue", "when": "after_response"}],
                                      "env": {"ITER_REPEAT_STEPS": "3", "ITER_PROGRESS_EVERY": "100"}})
        self.assertEqual(s["calls"], 6, log[-1500:])
        self.assertIn("stopped by a runaway backstop", " ".join(calls[3]["recent_user"]))

    def test_background_branch_progress_and_final(self):
        s, calls, log = run_scenario({"policy": "finish_at", "finish": 36, "slow_first": 1.6, "idle_limit": 200,
                                      "events": [{"text": "long task"}], "env": BRANCHES}, timeout=400)
        self.assertIn("LLM_PROMOTED", log)
        self.assertNotIn("BRANCH_CHECKPOINT_QUEUED", log)  # old code: stop + checkpoint after 25 branch steps
        notes = list(s["progress"].values())
        self.assertEqual(len(notes), 1, (notes, log[-2000:]))
        self.assertIn("Still working on this (step 32), not paused", notes[0])
        self.assertIn("BRANCH_PROGRESS_FALLBACK", log)
        self.assertEqual(list(s["outbox"].values()), ["final"])
        self.assertTrue(any(c["thread"] != "MainThread" for c in calls[5:]))

    def test_background_branch_ceiling_stop(self):
        s, calls, log = run_scenario({"policy": "work_forever", "slow_first": 1.6, "idle_limit": 200,
                                      "events": [{"text": "long task"}],
                                      "env": dict(BRANCHES, ITER_HARD_STEP_CEILING="15", ITER_PROGRESS_EVERY="100")}, timeout=300)
        self.assertIn("BRANCH_BACKSTOP", log)
        # 15 request steps, then only the single throttled post-task check-in (existing behaviour)
        self.assertEqual(s["calls"], 16, log[-1500:])
        self.assertIn("TASK COMPLETED", " ".join(calls[15]["recent_user"]))
        self.assertIn("step ceiling (15 steps)", " ".join(s["outbox"].values()))
        self.assertEqual(s["processing"], {})


def _load_channel(root):
    os.environ["ITER_OUTER_CHANNEL_ROOT"] = str(root)
    os.environ["ITER_OUTER_IDENTITY"] = "ProtoCosmo2"
    os.environ["ITER_MAX_OPEN_REQUESTS"] = "3"
    from src import iter_outer_channel_protocosmo2 as channel
    from src import iter_producer as producer
    from src import iter_responder as responder
    return channel, producer, responder


class ChannelDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("ITER_OUTER_CHANNEL_ROOT", "ITER_OUTER_IDENTITY",
                                                       "ITER_MAX_OPEN_REQUESTS", "ITER_RESPONDER_MAX_SECONDS")}
        self.channel, self.producer, self.responder = _load_channel(Path(self.tmp.name).resolve() / "ch")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def claim(self, text="long", message_id=77):
        rid = self.producer.submit(text, chat_id=42, message_id=message_id, update_id=9)
        self.assertEqual(self.channel.receive_request()["request_id"], rid)
        return rid

    def test_expire_stale_clock_restarts_on_progress(self):
        rid = self.claim()
        old = time.time() - 7200
        os.utime(self.channel._root() / "processing" / (rid + ".json"), (old, old))
        self.channel.send("still working", final=False, request_id=rid)
        self.assertFalse(self.channel.expire_stale(3600))
        self.assertIn(rid, self.channel.active_request_ids())
        rid2 = self.claim("other", 78)
        os.utime(self.channel._root() / "processing" / (rid2 + ".json"), (old, old))
        self.assertTrue(self.channel.expire_stale(3600))
        self.assertEqual(self.channel.active_request_ids(), [rid])

    def test_responder_delivers_progress_as_reply_then_final(self):
        delivered = []
        respond = self.responder.make_responder(chat_id=42, timeout_seconds=5, poll_interval=0.02,
                                                deliver_progress=lambda text: delivered.append(text) or True)
        respond.bind(message_id=77, update_id=9)

        def iter_side():
            while not self.channel.receive_request():
                time.sleep(0.01)
            self.channel.send("[m]\nStill working on this (step 32), not paused", final=False)
            time.sleep(0.2)
            self.channel.send("[m]\nfinal answer")
        worker = threading.Thread(target=iter_side)
        worker.start()
        answer = respond("long task")
        worker.join()
        self.assertEqual(answer, "[m]\nfinal answer")
        self.assertEqual(delivered, ["[m]\nStill working on this (step 32), not paused"])
        root = self.channel._root()
        self.assertEqual(list((root / "progress").glob("*.json")), [])
        self.assertEqual(len(list((root / "progress_delivered").glob("*.json"))), 1)

    def test_responder_timeout_unchanged_without_progress(self):
        respond = self.responder.make_responder(chat_id=42, timeout_seconds=0.3, poll_interval=0.02)
        respond.bind(message_id=77, update_id=9)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            respond("no answer")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_responder_extends_while_progress_flows_and_is_capped(self):
        os.environ["ITER_RESPONDER_MAX_SECONDS"] = "1.5"
        respond = self.responder.make_responder(chat_id=42, timeout_seconds=0.4, poll_interval=0.02,
                                                deliver_progress=lambda text: True)
        respond.bind(message_id=77, update_id=9)
        stop = threading.Event()

        def iter_side():
            while not self.channel.receive_request():
                time.sleep(0.01)
            while not stop.is_set():
                try:
                    self.channel.send("tick", final=False)
                except self.channel.ChannelContractError:
                    return
                time.sleep(0.25)
        worker = threading.Thread(target=iter_side)
        worker.start()
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            respond("endless")
        elapsed = time.monotonic() - started
        stop.set()
        worker.join()
        self.assertGreater(elapsed, 1.0)  # well past the 0.4 s base timeout
        self.assertLess(elapsed, 2.5)     # but capped at ITER_RESPONDER_MAX_SECONDS


class ReceiverWiringTest(unittest.TestCase):
    def load(self):
        source = RUNNER.read_text(encoding="utf-8")
        tree = ast.parse(source)
        ns = {"json": json, "hashlib": hashlib, "threading": threading,
              "ITER_PROGRESS_TELEGRAM_MAX_CHARS": 4000, "ITER_PROGRESS_MAX_ATTEMPTS": 3}
        exec(seg("make_iter_progress_deliverer", source=source, tree=tree), ns)
        return source, ns

    def test_runner_passes_deliver_progress(self):
        source, _ = self.load()
        self.assertIn("deliver_progress=make_iter_progress_deliverer(api, _inbound_ids)", source)

    def test_delivers_reply_to_bound_message(self):
        _, ns = self.load()
        sent = []
        api = SimpleNamespace(send_message=lambda **kw: sent.append(kw) or "555")
        ids = threading.local()
        deliver = ns["make_iter_progress_deliverer"](api, ids)
        self.assertFalse(deliver("x"))  # unbound thread: keep for retry
        ids.value = {"chat_id": -100, "message_id": 77, "update_id": 9}
        self.assertTrue(deliver("[m]\nStill working"))
        self.assertTrue(deliver("y" * 5000))
        self.assertEqual(sent[0], {"chat_id": -100, "text": "[m]\nStill working", "reply_to_message_id": 77})
        self.assertLessEqual(len(sent[1]["text"]), 4000)

    def test_failed_delivery_retried_then_dropped(self):
        _, ns = self.load()

        def boom(**kw):
            raise RuntimeError("telegram_URLError")
        ids = threading.local()
        ids.value = {"chat_id": -100, "message_id": 77, "update_id": 9}
        deliver = ns["make_iter_progress_deliverer"](SimpleNamespace(send_message=boom), ids)
        self.assertEqual([deliver("n"), deliver("n"), deliver("n")], [False, False, True])


if __name__ == "__main__":
    unittest.main()
