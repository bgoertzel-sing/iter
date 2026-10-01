"""Model tag: "[model]" on outgoing sends names the model that actually answered.

iter.py runs its main loop at import time, so never `import iter`; extract with ast.
"""
import ast
import json
import os
import queue
import re
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SRC = (HERE / "iter.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
HELPERS = ("_openclaw_agent_id", "_with_model_tag_session", "_remember_model_tag_session",
           "_tracked_llm_create", "_gateway_root", "served_model_from_history",
           "format_model_tag", "served_model_tag", "apply_model_tag", "tag_send_arguments")


def seg(*names):
    out = []
    for name in names:
        node = next(n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
        out.append(ast.get_source_segment(SRC, node))
    return "\n\n".join(out)


def load(*names, **extra):
    ns = {"os": os, "re": re, "json": json, "time": time, "uuid": uuid, "threading": threading,
          "MODEL": "openclaw/protocosmo2", "BASE_URL": "http://127.0.0.1:18789/v1", "API_KEY": "k",
          "MODEL_TAG_ENABLED": True, "MODEL_TAG_LOOKUP_TIMEOUT": 1, "MODEL_TAG_LOOKUP_ATTEMPTS": 2,
          "_MODEL_TAG_SESSION_KEYS": {}, "_MODEL_TAG_CACHE": {}, "_model_tag_lock": threading.Lock(),
          "_MODEL_TAG_MAX_ENTRIES": 512, **extra}
    exec(seg(*HELPERS), ns)
    exec(seg(*names), ns) if names else None
    ns["time"] = SimpleNamespace(sleep=lambda s: None, time=time.time)
    return ns


def assistant(run_id, model, stop="toolUse", provider="anthropic", error=None):
    m = {"role": "assistant", "provider": provider, "model": model, "stopReason": stop,
         "__openclaw": {"runId": run_id}}
    if error:
        m["errorMessage"] = error
    return m


FALLBACK_HISTORY = {"messages": [
    {"role": "user", "content": "hi", "__openclaw": {"runId": "chatcmpl_a"}},
    assistant("chatcmpl_a", "claude-opus-5-5", stop="error", error="overloaded"),
    {"role": "toolResult", "__openclaw": {"runId": "chatcmpl_a"}},
    assistant("chatcmpl_a", "claude-opus-4-6", stop="toolUse"),
]}


class FormatTest(unittest.TestCase):
    def test_names(self):
        f = load()["format_model_tag"]
        self.assertEqual(f("claude-opus-5-5"), "[claude-opus-5-5]")
        self.assertEqual(f("anthropic/claude-opus-4-6"), "[claude-opus-4-6]")
        self.assertEqual(f("qwen/qwen3.8-27b"), "[qwen3.8-27b]")
        self.assertEqual(f("claude-3-5-sonnet-20241022"), "[claude-3-5-sonnet]")
        self.assertEqual(f("openai/gpt-4o-latest"), "[gpt-4o]")
        self.assertEqual(f("  claude-opus-5-5  "), "[claude-opus-5-5]")
        for bad in ("", "   ", None, 7, "x/"):
            self.assertEqual(f(bad), "")


class HistoryTest(unittest.TestCase):
    def test_fallback_picks_model_that_answered(self):
        f = load()["served_model_from_history"]
        self.assertEqual(f(FALLBACK_HISTORY, "chatcmpl_a"), "claude-opus-4-6")

    def test_other_run_error_only_and_malformed(self):
        f = load()["served_model_from_history"]
        self.assertIsNone(f(FALLBACK_HISTORY, "chatcmpl_other"))
        self.assertIsNone(f({"messages": [assistant("r", "m", stop="error")]}, "r"))
        self.assertIsNone(f({"messages": [assistant("r", "m", stop="aborted")]}, "r"))
        self.assertIsNone(f({"messages": [assistant("r", "")]}, "r"))
        self.assertIsNone(f({"nope": 1}, "r"))
        self.assertIsNone(f(None, "r"))
        self.assertIsNone(f(FALLBACK_HISTORY, None))


class SessionKeyTest(unittest.TestCase):
    def test_openclaw_alias_gets_fresh_gateway_shaped_key(self):
        ns = load()
        kw1, k1 = ns["_with_model_tag_session"]({"model": "openclaw/protocosmo2", "extra_headers": {"a": "b"}})
        kw2, k2 = ns["_with_model_tag_session"]({"model": "openclaw/protocosmo2"})
        self.assertRegex(k1, r"^agent:protocosmo2:openai:[0-9a-f-]{36}$")
        self.assertNotEqual(k1, k2)
        self.assertEqual(kw1["extra_headers"], {"a": "b", "x-openclaw-session-key": k1})

    def test_non_openclaw_or_disabled_untouched(self):
        ns = load()
        kw = {"model": "gpt-4o"}
        self.assertEqual(ns["_with_model_tag_session"](kw), (kw, None))
        ns["MODEL_TAG_ENABLED"] = False
        kw = {"model": "openclaw/protocosmo2"}
        self.assertEqual(ns["_with_model_tag_session"](kw), (kw, None))

    def test_tracked_create_records_key_by_response_id(self):
        ns = load()
        seen = {}

        def create(**kw):
            seen.update(kw)
            return SimpleNamespace(id="chatcmpl_x", model="openclaw/protocosmo2")
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        r = ns["_tracked_llm_create"](client, model="openclaw/protocosmo2", messages=[])
        self.assertEqual(ns["_MODEL_TAG_SESSION_KEYS"]["chatcmpl_x"], seen["extra_headers"]["x-openclaw-session-key"])
        self.assertEqual(r.id, "chatcmpl_x")


class ServedTagTest(unittest.TestCase):
    def ns_with(self, run_id="chatcmpl_a"):
        ns = load()
        ns["_MODEL_TAG_SESSION_KEYS"][run_id] = "agent:protocosmo2:openai:k"
        return ns

    def test_actual_model_after_fallback_not_requested_alias(self):
        ns = self.ns_with()
        calls = []
        r = SimpleNamespace(id="chatcmpl_a", model="openclaw/protocosmo2")
        tag = ns["served_model_tag"](r, fetch_history=lambda k: calls.append(k) or FALLBACK_HISTORY)
        self.assertEqual(tag, "[claude-opus-4-6]")
        self.assertEqual(calls, ["agent:protocosmo2:openai:k"])
        # cached: no second lookup
        self.assertEqual(ns["served_model_tag"](r, fetch_history=lambda k: 1 / 0), "[claude-opus-4-6]")

    def test_unknown_gives_no_tag(self):
        ns = self.ns_with()
        r = SimpleNamespace(id="chatcmpl_a", model="openclaw/protocosmo2")
        self.assertEqual(ns["served_model_tag"](r, fetch_history=lambda k: {"messages": []}), "")
        ns = self.ns_with()
        self.assertEqual(ns["served_model_tag"](r, fetch_history=lambda k: 1 / 0), "")
        ns = load()  # no recorded session key: alias in response.model is never used
        self.assertEqual(ns["served_model_tag"](r, fetch_history=lambda k: FALLBACK_HISTORY), "")
        self.assertEqual(ns["served_model_tag"](None), "")
        self.assertEqual(ns["served_model_tag"](SimpleNamespace(id=None)), "")

    def test_retry_until_transcript_visible(self):
        ns = self.ns_with()
        replies = [{"messages": []}, FALLBACK_HISTORY]
        r = SimpleNamespace(id="chatcmpl_a")
        self.assertEqual(ns["served_model_tag"](r, fetch_history=lambda k: replies.pop(0)), "[claude-opus-4-6]")

    def test_vm2_served_model_field_honoured(self):
        ns = load()
        r = SimpleNamespace(id="x", served_model="anthropic/claude-opus-4-6-20260101")
        self.assertEqual(ns["served_model_tag"](r), "[claude-opus-4-6]")

    def test_disabled(self):
        ns = self.ns_with()
        ns["MODEL_TAG_ENABLED"] = False
        self.assertEqual(ns["served_model_tag"](SimpleNamespace(id="chatcmpl_a"), fetch_history=lambda k: FALLBACK_HISTORY), "")


class ApplyTest(unittest.TestCase):
    def test_no_double_tag_and_empty(self):
        f = load()["apply_model_tag"]
        self.assertEqual(f("hello", "[m]"), "[m]\nhello")
        self.assertEqual(f(f("hello", "[m]"), "[m]"), "[m]\nhello")
        self.assertEqual(f("[m] already", "[m]"), "[m] already")
        self.assertEqual(f("hello", ""), "hello")
        self.assertEqual(f("", "[m]"), "")
        self.assertEqual(f("   ", "[m]"), "   ")
        self.assertIsNone(f(None, "[m]"))

    def test_tag_send_arguments(self):
        ns = load()
        ns["served_model_tag"] = lambda r: r
        f = ns["tag_send_arguments"]
        args = {"channel": "protocosmo2", "content": "hi", "final": False}
        self.assertEqual(f("send", args, "[m]"), {"channel": "protocosmo2", "content": "[m]\nhi", "final": False})
        self.assertEqual(args["content"], "hi")  # original (experience) untouched
        self.assertIs(f("query", args, "[m]"), args)
        self.assertIs(f("send", args, ""), args)
        self.assertEqual(f("send", {"content": "[m]\nhi"}, "[m]"), {"content": "[m]\nhi"})
        self.assertEqual(f("send", {"content": ""}, "[m]"), {"content": ""})
        self.assertEqual(f("send", "notadict", "[m]"), "notadict")


def resp(tool_calls, rid):
    msg = SimpleNamespace(tool_calls=tool_calls, content=None,
                          model_dump=lambda exclude_none=True: {"role": "assistant", "content": ""})
    return SimpleNamespace(id=rid, choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")])


def send_call(content, cid="c1"):
    return SimpleNamespace(id=cid, function=SimpleNamespace(
        name="send", arguments=json.dumps({"channel": "protocosmo2", "content": content})))


class PathTest(unittest.TestCase):
    def test_background_branch_send_tagged_with_its_response_model(self):
        sent = []
        ns = load()
        ns["_MODEL_TAG_SESSION_KEYS"].update({"chatcmpl_b1": "k1", "chatcmpl_b2": "k2"})
        histories = {"k1": {"messages": [assistant("chatcmpl_b1", "claude-opus-4-6")]},
                     "k2": {"messages": [assistant("chatcmpl_b2", "qwen/qwen3.8-27b", stop="stop")]}}
        ns["_fetch_session_history"] = lambda k: histories[k]
        follow = [resp([send_call("second")], "chatcmpl_b2"), resp(None, "chatcmpl_b3")]

        class RC(dict):
            pass
        ns.update({"_merge_queue": queue.Queue(), "_tool_context": threading.local(),
                   "_release_branch": lambda *a: True,
                   "load_tools": lambda: ({"send": ("tools/send.py",)}, [], []), "native_tools": lambda i: [],
                   "invoke_dynamic": lambda path, fn, **kw: sent.append(kw["content"]) or {"ok": True, "result": "SUCCESS"},
                   "MAX_TOOL_CALLS": 5, "MAX_TOOL_OUTPUT_CHARS": 1000, "get_current_time": lambda: "t",
                   "KEEP_REASONING_IN_EPISODE": False, "BRANCH_STEP_BUDGET": 25, "MODEL": "openclaw/protocosmo2",
                   "MAX_TOKENS": 10, "extract_tier1_checkpoint": lambda *a: {},
                   "_bg_llm_create": lambda c, rc, **kw: follow.pop(0),
                   "record_branch_error": lambda e: None})
        exec(seg("_bg_branch_mini_loop"), ns)
        ns["_bg_branch_mini_loop"]("bg-t", None, [], resp([send_call("first")], "chatcmpl_b1"), RC())
        self.assertEqual(sent, ["[claude-opus-4-6]\nfirst", "[qwen3.8-27b]\nsecond"])

    def test_foreground_and_background_invoke_use_tag(self):
        main = SRC[SRC.index("while True:\n    try:"):] if "while True:\n    try:" in SRC else SRC
        self.assertIn('invoke_dynamic(INOPS[tool_name][0], "run", **tag_send_arguments(tool_name, tool_arguments, response))', SRC)
        self.assertIn('invoke_dynamic(inops[tool_name][0], "run", **tag_send_arguments(tool_name, tool_arguments, response))', SRC)
        self.assertNotIn('invoke_dynamic(INOPS[tool_name][0], "run", **tool_arguments)', SRC)
        self.assertNotIn('invoke_dynamic(inops[tool_name][0], "run", **tool_arguments)', SRC)
        self.assertIn("send_text_only_fallback(last_text_only_content, served_model_tag(response))", SRC)
        self.assertIn("send_checkpoint_message(_checkpoint_data, _checkpoint_path, served_model_tag(response))", SRC)
        self.assertIn('checkpoint_data["_model_tag"] = served_model_tag(response)', SRC)
        self.assertIn('send_checkpoint_message, cp_data, cp_path, cp.get("_model_tag", ""))', SRC)
        # every LLM call goes through the tracked create
        self.assertEqual(SRC.count("client.chat.completions.create("), 1)  # only inside _tracked_llm_create

    def test_text_only_fallback_and_checkpoint_tagged(self):
        sent = []
        ns = load("send_text_only_fallback", "send_checkpoint_message", "format_checkpoint_message",
                  TEXT_ONLY_FALLBACK_MAX_CHARS=3500, CHECKPOINT_CHANNEL="protocosmo2", Path=Path,
                  invoke_dynamic=lambda p, fn, **kw: sent.append(kw["content"]) or {"ok": True, "result": "SUCCESS"})
        self.assertTrue(ns["send_text_only_fallback"]("plain answer", "[claude-opus-5-5]"))
        self.assertTrue(ns["send_text_only_fallback"]("no tag known", ""))
        self.assertTrue(ns["send_checkpoint_message"]({"step_count": 3, "tool_snapshot": []}, None, "[claude-opus-4-6]"))
        self.assertTrue(ns["send_checkpoint_message"]({"step_count": 3, "tool_snapshot": []}, None))
        self.assertEqual(sent[0], "[claude-opus-5-5]\nplain answer")
        self.assertEqual(sent[1], "no tag known")
        self.assertTrue(sent[2].startswith("[claude-opus-4-6]\n[CHECKPOINT]"))
        self.assertTrue(sent[3].startswith("[CHECKPOINT]"))


if __name__ == "__main__":
    unittest.main()
