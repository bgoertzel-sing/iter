import importlib.util
import inspect
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
    if final:
        module.send(content)
        return "SUCCESS"
    if "final" not in inspect.signature(module.send).parameters:
        return f"Channel {channel} does not support progress messages (final=false)"
    module.send(content, final=False)
    return "SUCCESS (progress; request still open)"
