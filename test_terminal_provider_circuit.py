import ast
from pathlib import Path
import re


SOURCE = Path(__file__).with_name("iter.py")


def _load_function(name):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {
        "TERMINAL_PROVIDER_STATUS_CODES": frozenset({400, 401, 402, 403, 413}),
        "re": re,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace[name]


def test_terminal_provider_status_reads_direct_status():
    classify = _load_function("terminal_provider_status")

    class ProviderError(Exception):
        status_code = 402

    assert classify(ProviderError("payment required")) == 402


def test_terminal_provider_status_reads_wrapped_background_error():
    classify = _load_function("terminal_provider_status")
    error = Exception(
        "Background LLM call failed: APIStatusError: Error code: 402 - "
        "{'error': {'message': 'Prompt tokens limit exceeded'}}"
    )
    assert classify(error) == 402


def test_retryable_provider_error_is_not_terminal():
    classify = _load_function("terminal_provider_status")
    assert classify(Exception("Error code: 429 - rate limited")) is None
    assert classify(Exception("temporary network timeout")) is None


def test_main_loop_breaks_terminal_error_before_generic_retry():
    source = SOURCE.read_text(encoding="utf-8")
    breaker = source.index("if send_terminal_provider_failure(error):")
    retry = source.index("experience = experience[:history_checkpoint]", breaker)
    assert breaker < retry
    assert "pending_event_append = slow_wait_for_input()" in source[breaker:retry]


def test_terminal_breaker_is_handled_even_when_notice_fails():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "send_terminal_provider_failure")
    namespace = {
        "Path": Path,
        "CHECKPOINT_CHANNEL": "test",
        "terminal_provider_status": lambda error: 402,
        "invoke_dynamic": lambda *args, **kwargs: {
            "ok": False,
            "result": "delivery failed",
        },
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), namespace)
    assert namespace["send_terminal_provider_failure"](Exception("402")) is True
