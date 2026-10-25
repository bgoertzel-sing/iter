import importlib.util
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

DESCRIPTION = ("Create a reminder that fires automatically at a UTC time and is delivered as a reply "
               "to the message you are handling. Usage: run(when='2026-08-19 09:00:00', "
               "channel='<your channel>', message='Submit NeurIPS paper')")

MAX_PENDING = 20
MAX_AHEAD_SECONDS = 30 * 24 * 3600


def _source(root, channel):
    path = root / "channels" / (channel + ".py")
    if not path.is_file():
        raise ValueError(f"Unknown channel: {channel}")
    spec = importlib.util.spec_from_file_location("channel_" + channel, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "reminder_source"):
        raise ValueError(f"Channel {channel} cannot deliver reminders")
    raw = os.environ.get("ITER_REQUEST_IDS")  # set by Iter when request binding is on
    if raw is None:
        return json.loads(module.reminder_source())
    try:
        ids = json.loads(raw)
    except ValueError:
        ids = {}
    request_id = ids.get(channel) if isinstance(ids, dict) else None
    if request_id is None:
        raise ValueError(f"No {channel} request is bound to this turn to attach the reminder to")
    return json.loads(module.reminder_source(request_id=request_id))


def run(when, channel="terminal", message=""):
    """
    when: str - UTC datetime 'YYYY-MM-DD HH:MM:SS'
    channel: str - your channel (the reminder replies to the message you are handling)
    message: str - reminder text
    Writes memory/alarms/<unix-ts>-<id>.json; Iter delivers it when due.
    """
    root = Path(__file__).resolve().parent.parent
    alarms = root / "memory" / "alarms"
    alarms.mkdir(parents=True, exist_ok=True)
    message = str(message).strip()
    if not message or len(message) > 2000:
        return "Reminder not set: message must be 1-2000 characters"
    due = int(datetime.strptime(when, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    now = datetime.now(timezone.utc).timestamp()
    if due < now - 60:
        return f"Reminder not set: {when} UTC is in the past"
    if due > now + MAX_AHEAD_SECONDS:
        return "Reminder not set: at most 30 days ahead"
    if len([p for p in alarms.iterdir() if p.is_file() and p.name[:1].isdigit()]) >= MAX_PENDING:
        return f"Reminder not set: {MAX_PENDING} reminders already pending"
    source = _source(root, channel)
    alarm = alarms / f"{due}-{uuid.uuid4().hex[:8]}.json"
    alarm.write_text(json.dumps({"due": due, "channel": channel, "message": message, "source": source}))
    return (f"SUCCESS, RETURN: Reminder set for {when} UTC -> {alarm.name}. It will be delivered "
            "automatically as a reply to this conversation; do not send it yourself.")
