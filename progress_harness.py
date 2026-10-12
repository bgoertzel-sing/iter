#!/usr/bin/env python3
"""Provider-free harness (PROGRESS_NOT_PAUSE, 2026-10-04): run ProtoCosmo2's real iter.py
main loop against a scripted fake OpenAI client, the real filesystem outer channel
(../src), the real channels/protocosmo2.py shim and tools/send.py + tools/nop.py, in a
temp tree.  Not a test module (pytest does not collect it).

Usage: progress_harness.py TMP_DIR SCENARIO_JSON
Env: HARNESS_ITER_PY overrides the iter.py under test (negative control on old code).
Writes TMP_DIR/calls.jsonl and TMP_DIR/summary.json.
"""
import hashlib
import json
import os
import runpy
import shutil
import sys
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent / "src"
tmp, scenario = Path(sys.argv[1]).resolve(), json.loads(sys.argv[2])
iter_py = Path(os.environ.get("HARNESS_ITER_PY", HERE / "iter.py")).resolve()
(tmp / "src").mkdir(parents=True)
(tmp / "src" / "__init__.py").write_text("")
for name in ("iter_outer_channel_protocosmo2.py", "iter_producer.py", "iter_relay.py", "iter_responder.py"):
    shutil.copy(SRC_DIR / name, tmp / "src")
repo = tmp / "repos"
for sub in ("channels", "tools", "memory", "transformations"):
    (repo / sub).mkdir(parents=True)
shutil.copy(iter_py, repo / "iter.py")
shutil.copy(HERE / "prompt.txt", repo)
shutil.copy(HERE / "channels" / "protocosmo2.py", repo / "channels")
shutil.copy(HERE / "tools" / "send.py", repo / "tools")
shutil.copy(HERE / "tools" / "nop.py", repo / "tools")
(repo / "tools" / "work.py").write_text('DESCRIPTION = "test work"\n\ndef run(arg):\n    return "result:" + arg\n')
fake = tmp / "fake" / "openai"
fake.mkdir(parents=True)
(fake / "__init__.py").write_text("class OpenAI:\n    def __init__(self, **kw):\n        pass\n"
                                  "class APITimeoutError(Exception):\n    pass\n"
                                  "class APIConnectionError(Exception):\n    pass\n")
channel_root = tmp / "state" / "iter-channel"
os.environ.update({"PYTHONPATH": str(tmp / "fake"),"ITER_OUTER_IDENTITY": "ProtoCosmo2", "ITER_OUTER_CHANNEL_ROOT": str(channel_root),
                   "ITER_MODEL_TAG": "0", "LLM_MODEL": "fake/model", "AI_API_KEY": "dummy"})
os.environ.update(scenario.get("env", {}))
sys.path.insert(0, str(tmp))
from src import iter_outer_channel_protocosmo2 as channel  # noqa: E402

channel._root()
events = list(scenario.get("events", []))
calls_log = open(tmp / "calls.jsonl", "w")
state = {"calls": 0, "idle_sleeps": 0, "submitted": 0}
lock = threading.Lock()


def submit(text):
    state["submitted"] += 1
    request_id = hashlib.sha256(f"req-{state['submitted']}-{text}".encode()).hexdigest()
    source = {"chat_id": 42, "message_id": 1000 + state["submitted"], "update_id": 5000 + state["submitted"]}
    payload = {"schema_version": 1, "identity": "ProtoCosmo2", "request_id": request_id, "prompt": text,
               "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(), "source": source}
    channel._atomic_write(channel_root / "inbox" / (request_id + ".json"), payload)
    return request_id


def maybe_inject():
    if not events:
        return False
    when = events[0].get("when", "start")
    open_now = list((channel_root / "processing").glob("*.json"))
    answered = list((channel_root / "outbox").glob("*.json"))
    if when == "start" or (when == "after_response" and answered and not open_now):
        submit(events.pop(0)["text"])
        return True
    return False


maybe_inject()


class Obj(types.SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments}}
                               for c in self.tool_calls]}


def last_user(messages):
    texts = [m.get("content") for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
    return texts[-3:]


def policy(i, messages):
    name = scenario["policy"]
    recent = " ".join(last_user(messages))
    if name == "work_forever":
        return [("work", {"arg": f"x{i}"})]
    if name == "work_obey_progress":
        if "PROGRESS CHECKPOINT" in recent and state.get("obeyed") != recent:
            state["obeyed"] = recent
            return [("send", {"channel": "protocosmo2", "content": f"model note at call {i}", "final": False}),
                    ("work", {"arg": f"x{i}"})]
        return [("work", {"arg": f"x{i}"})]
    if name == "repeat_same":
        return [("work", {"arg": "same"})]
    if name == "cycle_two":
        return [("work", {"arg": "a" if i % 2 else "b"})]
    if name == "reply_nop":
        return [("send", {"channel": "protocosmo2", "content": "hi"}), ("nop", {})]
    if name == "finish_at":
        if i >= scenario["finish"]:
            return [("send", {"channel": "protocosmo2", "content": "final"}), ("nop", {})]
        return [("work", {"arg": f"x{i}"})]
    raise SystemExit("unknown policy")


def create(**kwargs):
    with lock:
        state["calls"] += 1
        state["idle_sleeps"] = 0
        i = state["calls"]
    if i == 1 and scenario.get("slow_first"):
        real_sleep(float(scenario["slow_first"]))  # force promotion to a background branch
    messages = kwargs["messages"]
    calls_log.write(json.dumps({"call": i, "thread": threading.current_thread().name,
                                "recent_user": last_user(messages)}) + "\n")
    calls_log.flush()
    if i > scenario.get("max_calls", 200):
        state["over"] = True
        raise KeyboardInterrupt("max_calls")  # not an Exception: never retried
    calls = [Obj(id=f"c{i}_{k}", function=Obj(name=n, arguments=json.dumps(a)))
             for k, (n, a) in enumerate(policy(i, messages))]
    message = Obj(content=None, tool_calls=calls, role="assistant")
    return Obj(id=f"chatcmpl_{i}", choices=[Obj(message=message, finish_reason="tool_calls")], usage=None)


fake_openai = types.ModuleType("openai")


class OpenAI:
    def __init__(self, **kwargs):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))


class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


fake_openai.OpenAI = OpenAI
fake_openai.APITimeoutError = APITimeoutError
fake_openai.APIConnectionError = APIConnectionError
sys.modules["openai"] = fake_openai

real_sleep = time.sleep


def fake_sleep(seconds):
    # Only iter.py's own waits are virtualised (short real sleep so branch threads
    # progress); subprocess polling keeps real timing.
    if not sys._getframe(1).f_code.co_filename.endswith("iter.py"):
        return real_sleep(seconds)
    real_sleep(min(seconds, 0.01))
    if threading.current_thread() is not threading.main_thread():
        return
    state["idle_sleeps"] += 1
    if maybe_inject():
        state["idle_sleeps"] = 0
    if state.get("over"):
        raise SystemExit("max_calls")
    if state["idle_sleeps"] > scenario.get("idle_limit", 40):
        raise SystemExit("idle")


time.sleep = fake_sleep
os.chdir(repo)
sys.argv = [str(repo / "iter.py")]
reason = "returned"
try:
    runpy.run_path(str(repo / "iter.py"), run_name="__main__")
except (SystemExit, KeyboardInterrupt) as stop:
    reason = str(stop).strip("'")


def dump(directory):
    out = {}
    for path in sorted((channel_root / directory).glob("*.json")):
        value = json.loads(path.read_text())
        out[path.name] = value.get("response", value.get("prompt", ""))
    return out


summary = {"stop": reason, "calls": state["calls"],
           **{d: dump(d) for d in ("inbox", "processing", "outbox", "completed", "progress")}}
(tmp / "summary.json").write_text(json.dumps(summary, indent=1))
print("HARNESS_DONE", json.dumps({"stop": reason, "calls": state["calls"]}), flush=True)
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)  # do not wait for daemon branch threads
