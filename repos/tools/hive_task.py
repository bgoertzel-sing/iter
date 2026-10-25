import json
import os
import time
from pathlib import Path

DESCRIPTION = ("Hive record actions; the hive channel tells you of tasks. action: propose (a task someone asked "
               "for in chat: text = title line, then what is wanted; extra = where, e.g. telegram:<chat>/<msg>), "
               "accept, progress or checkpoint (text), block (text = question; extra = decision, information, access "
               "or external), unblock, result (text = report; push code first), review (text = findings; extra = "
               "passed, failed or needs_information). task = id (empty: your open task). Outcomes come on the hive "
               "channel.")

ACTIONS = ("propose", "accept", "progress", "checkpoint", "block", "unblock", "result", "review")


def run(action, task, text, extra=""):
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        return f"FAILED: action must be one of {', '.join(ACTIONS)}"
    root = Path(__file__).resolve().parent.parent
    if not (root / ".hive" / "config.json").is_file():
        return "FAILED: the hive channel isn't configured (no .hive/config.json)"
    out = root / ".runtime" / "hive" / "outbox"
    out.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{os.getpid()}"
    tmp = out / f".{name}.tmp"
    tmp.write_text(json.dumps({"action": action, "task": (task or "").strip(), "text": text or "",
                               "extra": (extra or "").strip()}))
    tmp.replace(out / name)
    what = "a proposal" if action == "propose" else (task or "(your open task)")
    return f"QUEUED: {action} {what}; the outcome arrives on the hive channel"
