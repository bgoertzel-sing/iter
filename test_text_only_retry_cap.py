"""TEXT_ONLY_RETRY_CAP: how many text-only (no tool call) replies the main loop re-prompts.

The decision block is inline in the main loop (iter.py runs its loop at import), so it is
ast-extracted and run inside a small loop that feeds it fake responses.
Gap found by the 2026-10-05 long-request audit (experiments/20261005T2023Z-iter-long-request-audit).
"""
import ast
import textwrap
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).with_name("iter.py")
SRC = SOURCE.read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _cap():
    node = next(n for n in TREE.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "TEXT_ONLY_RETRY_CAP" for t in n.targets))
    return ast.literal_eval(node.value)


def _decision_block():
    """The `if response.choices[0].finish_reason != "length":` block of the main loop."""
    for node in ast.walk(TREE):
        if (isinstance(node, ast.If)
                and ast.unparse(node.test) == "response.choices[0].finish_reason != 'length'"
                and "TEXT_ONLY_IDLE" in ast.get_source_segment(SRC, node)):
            return textwrap.dedent(ast.get_source_segment(SRC, node, padded=True))
    raise AssertionError("text-only decision block not found in iter.py")


def _simulate(post_task_mode, finish_reason="stop", limit=20):
    """Feed text-only replies until the block breaks out (or `limit` calls)."""
    harness = (
        "def run(post_task_mode, response, TEXT_ONLY_RETRY_CAP, limit):\n"
        "    text_only_idle, text_only_retries, calls = False, 0, 0\n"
        "    while calls < limit:\n"
        "        calls += 1\n"
        + textwrap.indent(_decision_block(), " " * 8) + "\n"
        "    return calls, text_only_idle, text_only_retries\n"
    )
    ns = {}
    exec(compile(harness, str(SOURCE), "exec"), ns)
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason)])
    return ns["run"](post_task_mode, response, _cap(), limit)


def test_cap_is_two():
    assert _cap() == 2


def test_user_request_reprompts_cap_times_then_idles():
    calls, idle, retries = _simulate(post_task_mode=False)
    assert (calls, idle, retries) == (_cap() + 1, True, _cap())


def test_autonomous_turn_idles_on_first_text_only_reply():
    calls, idle, retries = _simulate(post_task_mode=True)
    assert (calls, idle, retries) == (1, True, 0)


def test_length_cutoff_does_not_use_up_the_cap():
    # finish_reason == "length" is handled by the OUTPUT TOKEN LIMIT re-prompt instead.
    calls, idle, retries = _simulate(post_task_mode=False, finish_reason="length", limit=6)
    assert (calls, idle, retries) == (6, False, 0)


def test_idle_path_delivers_text_for_unanswered_requests():
    # After TEXT_ONLY_IDLE, an unanswered user request gets the text-only fallback.
    after = SRC[SRC.index("if text_only_idle:"):]
    after = after[:after.index("continue") + len("continue")] if "continue" in after[:2000] else after[:2000]
    assert "if not post_task_mode and not send_since_checkpoint:" in after
    assert "send_text_only_fallback(last_text_only_content" in after
