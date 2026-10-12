"""Regression (2026-10-01): iter.py --invoke must keep channel-contract hook results
JSON-native; receive()/resume_bound() index receive_request()["request_id"] etc."""
import json
import subprocess
import sys
from pathlib import Path

ITER = Path(__file__).resolve().parent / "iter.py"

CHANNEL = '''
def receive_request():
    return {"request_id": "ab" * 32, "prompt": "hello"}
def resume_all():
    return [{"request_id": "cd" * 32, "prompt": "p"}]
def active_request_ids():
    return ["ab" * 32]
def close_active(note="x", request_id=None):
    return False
def expire_stale(max_age_seconds, note="x", now=None):
    return False
def other():
    return {"a": 1}
'''


def _invoke(tmp_path, module, function, *args, **kwargs):
    result = tmp_path / (function + ".result.json")
    payload = tmp_path / (function + ".payload.json")
    payload.write_text(json.dumps({"args": args, "kwargs": kwargs}))
    subprocess.run([sys.executable, str(ITER), "--invoke", str(module), function, str(result), str(payload)],
                   cwd=ITER.parent, stdin=subprocess.DEVNULL, check=True, timeout=60)
    return json.loads(result.read_text())


def test_channel_hooks_return_structured_values(tmp_path):
    module = tmp_path / "fakechan.py"
    module.write_text(CHANNEL)
    assert _invoke(tmp_path, module, "receive_request") == {"ok": True, "result": {"request_id": "ab" * 32, "prompt": "hello"}}
    assert _invoke(tmp_path, module, "resume_all")["result"][0]["request_id"] == "cd" * 32
    assert _invoke(tmp_path, module, "active_request_ids")["result"] == ["ab" * 32]
    assert _invoke(tmp_path, module, "close_active", request_id="ab" * 32)["result"] is False
    assert _invoke(tmp_path, module, "expire_stale", 10)["result"] is False


def test_other_functions_still_stringified(tmp_path):
    module = tmp_path / "fakechan.py"
    module.write_text(CHANNEL)
    assert _invoke(tmp_path, module, "other")["result"] == "{'a': 1}"
