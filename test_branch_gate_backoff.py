import ast
import re
from pathlib import Path

import openai


SOURCE = Path(__file__).with_name("iter.py")


def _load_functions(*names, **extra):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        "re": re,
        "os": __import__("os"),
        "openai": openai,
        "RETRY_BACKOFF_BASE": 5,
        "RETRY_BACKOFF_CAP": 300,
        "RETRYABLE_PROVIDER_STATUS_CODES": frozenset({408, 409, 429, 500, 502, 503, 504, 529}),
        "QUOTA_EXHAUSTED_PATTERN": re.compile(
            r"usage limit|next reset in|insufficient_quota|quota exceeded|exceeded your current quota",
            re.IGNORECASE),
        **extra,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


def test_backoff_doubles_from_base_and_caps():
    delay = _load_functions("retry_backoff_delay")["retry_backoff_delay"]
    assert [delay(n, jitter=0) for n in range(1, 9)] == [5, 10, 20, 40, 80, 160, 300, 300]


def test_backoff_jitter_is_bounded_and_retry_after_is_honoured():
    delay = _load_functions("retry_backoff_delay")["retry_backoff_delay"]
    assert delay(1, jitter=1) == 5.5
    assert delay(1, retry_after=60, jitter=0) == 60
    assert delay(1, retry_after=10**6, jitter=0) == 3600


def test_retryable_classification():
    ns = _load_functions("retryable_provider_error")
    retryable = ns["retryable_provider_error"]

    class RateLimited(Exception):
        status_code = 429

    assert retryable(RateLimited("slow down"))
    assert retryable(Exception("Background LLM call failed: RateLimitError: Error code: 429 - {...}"))
    assert retryable(Exception("Background LLM call failed: APITimeoutError: Request timed out."))
    assert retryable(Exception("Error code: 503 - upstream unavailable"))
    assert not retryable(Exception("Error code: 402 - payment required"))
    assert not retryable(KeyError("choices"))


def test_quota_exhaustion_detection():
    exhausted = _load_functions("provider_quota_exhausted")["provider_quota_exhausted"]
    assert exhausted(Exception(
        "Error code: 429 - All models failed (2): You've reached your Codex subscription usage limit. "
        "Next reset in 3 days, Sep 26 at 1:19 AM PDT."))
    assert not exhausted(Exception("Error code: 429 - openai is asking us to slow down"))


def test_retry_after_header_parsing():
    retry_after = _load_functions("retry_after_seconds")["retry_after_seconds"]

    class Response:
        headers = {"retry-after": "12"}

    class Err(Exception):
        response = Response()

    assert retry_after(Err()) == 12.0
    assert retry_after(Exception("no response")) is None


def test_main_loop_polls_instead_of_calling_llm_while_branch_active():
    source = SOURCE.read_text(encoding="utf-8")
    loop = source[source.index("\nwhile True:"):]
    gate = loop.index("background_branch_active()")
    llm = loop.index('print("BEFORE LLM")')
    assert gate < llm
    assert "continue" in loop[gate:loop.index("if event_append:", gate)]


def test_promotion_never_overwrites_active_branch():
    source = SOURCE.read_text(encoding="utf-8")
    body = source[source.index("def threaded_llm_call"):source.index("_merge_queue = queue.Queue()")]
    skip = body.index("background_branch_active()")
    assign = body.index("_active_branch = branch")
    assert skip < assign


def test_abandon_stops_branch_work():
    source = SOURCE.read_text(encoding="utf-8")
    deadline = source[source.index("def check_background_deadline"):]
    assert 'branch.result_container["abandoned"] = True' in deadline
    mini = source[source.index("def _bg_branch_mini_loop"):source.index("def drain_merge_queue")]
    assert 'result_container.get("abandoned")' in mini


def test_branch_errors_reach_main_loop_before_receive():
    source = SOURCE.read_text(encoding="utf-8")
    assert source.count("record_branch_error(e)") == 2
    loop = source[source.index("\nwhile True:"):]
    assert loop.index("pop_branch_error()") < loop.index('print("BEFORE RECEIVE")')


def test_retryable_errors_back_off_instead_of_fixed_retry():
    source = SOURCE.read_text(encoding="utf-8")
    handler = source[source.rindex("    except Exception as error:"):]
    assert "retry_backoff_delay(" in handler
    assert "backoff_wait(delay)" in handler
    assert "provider_idle = True" in handler
