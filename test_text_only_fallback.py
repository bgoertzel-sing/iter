import ast
import json
import re
from pathlib import Path

SOURCE = Path(__file__).with_name("iter.py")

def _load(calls, result="SUCCESS"):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in ("send_text_only_fallback", "apply_model_tag", "unwrap_pseudo_send"))
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "_PSEUDO_SEND_RE" for t in n.targets))]
    def fake_invoke(path, fn, **kw):
        calls.append(kw)
        return {"ok": True, "result": result}
    ns = {"Path": Path, "json": json, "re": re, "invoke_dynamic": fake_invoke, "CHECKPOINT_CHANNEL": "protocosmo2",
          "TEXT_ONLY_FALLBACK_MAX_CHARS": 3500}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), ns)
    return ns["send_text_only_fallback"]

def test_fallback_posts_text():
    calls = []
    assert _load(calls)("hello glicerico") is True
    assert calls == [{"channel": "protocosmo2", "content": "hello glicerico"}]

def test_fallback_has_no_model_tag_parameter():  # REFERENCE BRANCH: model tag excluded (live: test_fallback_model_tag)
    calls = []
    assert _load(calls)("hello") is True
    assert calls[0]["content"] == "hello"

def test_fallback_skips_empty():
    calls = []
    f = _load(calls)
    assert f(None) is False and f("   ") is False and calls == []

def test_fallback_truncates_long_text():
    calls = []
    assert _load(calls)("x" * 5000) is True
    assert len(calls[0]["content"]) < 3600 and calls[0]["content"].endswith("[...truncated]")

def test_fallback_reports_channel_failure():
    assert _load([], result="ChannelContractError")("hi") is False

def test_main_loop_wires_fallback_only_for_unanswered_user_requests():
    src = SOURCE.read_text(encoding="utf-8")
    assert "if not post_task_mode and not send_since_checkpoint:" in src
    assert "send_text_only_fallback(last_text_only_content)" in src  # REFERENCE BRANCH: model tag excluded
    assert "last_text_only_content = message.content if not message.tool_calls else None" in src


def test_fallback_unwraps_send_written_as_text():
    calls = []
    text = 'tool_call id=c7e41a2b9 name=send arguments={"channel":"protocosmo2","final":"true","content":"line one\\nline two"}'
    assert _load(calls)(text) is True  # REFERENCE BRANCH: model tag excluded
    assert calls[0]["content"] == "line one\nline two"

def test_fallback_keeps_text_when_pseudo_send_is_malformed():
    calls = []
    text = 'tool_call name=send arguments={"content": "unterminated'
    assert _load(calls)(text) is True
    assert calls[0]["content"] == text

def test_branch_loop_delivers_unanswered_text_only_ending():
    src = SOURCE.read_text(encoding="utf-8")
    loop = src[src.index("def _bg_branch_mini_loop"):src.index("def threaded_llm_call")]
    assert "if not branch_sent and not result_container.get(\"abandoned\"):" in loop
    assert "send_text_only_fallback(message.content)" in loop  # REFERENCE BRANCH: model tag excluded
    assert "branch_sent = True" in loop
