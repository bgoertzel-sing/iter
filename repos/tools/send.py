import importlib.util
import inspect
import json
import os
from pathlib import Path

DESCRIPTION = "Send a message through a communication channel."


def _bound_request_id(channel):
    """(bound, request_id) from ITER_REQUEST_IDS, which Iter sets when request binding is on.

    bound=False: legacy mode, the channel answers its single open request.
    bound=True, request_id=None: this turn/branch owns no request on the channel.
    """
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
    if not hasattr(module, "send"):
        return f"Channel {channel} cannot send"
    bound, request_id = _bound_request_id(channel)
    if not bound:
        module.send(content)
        return "SUCCESS"
    if request_id is None:
        return (f"No {channel} request is bound to this turn, so there is nothing to answer. "
                "Background branches answer their own requests.")
    if "request_id" not in inspect.signature(module.send).parameters:
        return f"Channel {channel} cannot send to a bound request"
    module.send(content, request_id=request_id)
    return "SUCCESS"
