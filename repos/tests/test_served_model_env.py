"""A tool run for a response gets ITER_SERVED_MODEL, the model the gateway says wrote that response, per thread
(the foreground and each branch apart), and nothing when the response doesn't say (never a stale one).

    python3 repos/tests/test_served_model_env.py
"""
import ast
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

src = (Path(__file__).resolve().parent.parent / "iter.py").read_text()
tree = ast.parse(src)
wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_tool_env", "note_served")]
assert len(wanted) == 2
ns = {"os": os, "json": json, "_tool_context": threading.local()}
exec(compile(ast.Module(body=wanted, type_ignores=[]), "iter.py", "exec"), ns)
tool_env, note = ns["_tool_env"], ns["note_served"]
os.environ["ITER_SERVED_MODEL"] = "stale/from-parent"

assert tool_env() is None or "ITER_SERVED_MODEL" not in tool_env()
note(SimpleNamespace(served_model=" anthropic/claude-opus-5-5 "))
assert tool_env()["ITER_SERVED_MODEL"] == "anthropic/claude-opus-5-5"
assert "ITER_REQUEST_IDS" not in tool_env()
ns["_tool_context"].request_ids = {"hive": "r1"}
env = tool_env()
assert env["ITER_SERVED_MODEL"] == "anthropic/claude-opus-5-5" and json.loads(env["ITER_REQUEST_IDS"]) == {"hive": "r1"}
note(SimpleNamespace())                                   # the gateway didn't say: no model, not the last one
assert "ITER_SERVED_MODEL" not in tool_env()
note(SimpleNamespace(served_model="anthropic/claude-sonnet-5-5"))

seen = {}
def branch():
    assert "ITER_SERVED_MODEL" not in (tool_env() or {})  # another thread's model isn't this one's
    note(SimpleNamespace(served_model="openai/gpt-5"))
    seen["branch"] = tool_env()["ITER_SERVED_MODEL"]
t = threading.Thread(target=branch); t.start(); t.join()
assert seen["branch"] == "openai/gpt-5" and tool_env()["ITER_SERVED_MODEL"] == "anthropic/claude-sonnet-5-5"
print("PASSED: tools get the model that wrote their response, per thread, never a stale one")
