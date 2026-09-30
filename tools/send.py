import importlib.util
import inspect
import json
import os
from pathlib import Path

DESCRIPTION = ("Send a message through a communication channel. final defaults to true "
               "(answer and close the request). Use final=false for a progress message "
               "that keeps the request open; finish with a final=true message.")


def _parse_final(final):
    if isinstance(final, bool):
        return final
    if isinstance(final, str):
        value = final.strip().lower()
        if value in ("true", "1", "yes", ""):
            return True
        if value in ("false", "0", "no"):
            return False
    if isinstance(final, int):
        return bool(final)
    raise ValueError(f"final must be true or false, got {final!r}")


def _bound_request_id(channel):
    """(bound, request_id) from ITER_REQUEST_IDS, set by Iter when request binding is on.

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


def run(channel, content, final=True):
    """Send a message through the named communication channel."""
    path = Path("channels") / (channel + ".py")
    # Guard against path traversal — channel must be a simple name
    if "/" in channel or "\\" in channel or ".." in channel:
        return f"Invalid channel name: {channel}"
    if not path.is_file():
        return f"Unknown channel: {channel}"
    try:
        final = _parse_final(final)
    except ValueError as exc:
        return str(exc)
    spec = importlib.util.spec_from_file_location("channel_" + channel, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "send"):
        return f"Channel {channel} cannot send"
    parameters = inspect.signature(module.send).parameters
    kwargs = {}
    bound, request_id = _bound_request_id(channel)
    if bound and "request_id" in parameters:
        if request_id is None:
            return (f"No open {channel} request is bound to this turn, so there is nothing to answer. "
                    "Background branches answer their own requests.")
        kwargs["request_id"] = request_id
    if final:
        module.send(content, **kwargs)
        return "SUCCESS"
    if "final" not in parameters:
        return f"Channel {channel} does not support progress messages (final=false)"
    module.send(content, final=False, **kwargs)
    return "SUCCESS (progress; request still open)"
