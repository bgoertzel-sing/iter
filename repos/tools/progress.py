import importlib.util
import inspect
import json
import os
from pathlib import Path

DESCRIPTION = ("Post a short interim progress note (done / doing next / blockers) to the person on a channel "
               "WITHOUT ending your task: it does not answer or close the request and does not use one of your "
               "follow-up slots. Keep working afterwards. Use send for your reply and your final result.")


def _bound_request_id(channel):
    """(bound, request_id) from ITER_REQUEST_IDS (see tools/send.py)."""
    raw = os.environ.get("ITER_REQUEST_IDS")
    if raw is None:
        return False, None
    try:
        ids = json.loads(raw)
    except ValueError:
        return True, None
    return True, (ids.get(channel) if isinstance(ids, dict) else None)


def run(channel, content):
    if "/" in channel or "\\" in channel or ".." in channel:
        return f"Invalid channel name: {channel}"
    path = Path("channels") / (channel + ".py")
    if not path.is_file():
        return f"Unknown channel: {channel}"
    spec = importlib.util.spec_from_file_location("channel_" + channel, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "progress"):
        return f"Channel {channel} has no progress path; use send"
    bound, request_id = _bound_request_id(channel)
    if not bound:
        module.progress(content)
        return "SUCCESS"
    if request_id is None:
        return f"No {channel} request is bound to this turn, so there is nobody to update."
    if "request_id" not in inspect.signature(module.progress).parameters:
        return f"Channel {channel} cannot post progress to a bound request"
    module.progress(content, request_id=request_id)
    return "SUCCESS"
