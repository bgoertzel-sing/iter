from datetime import datetime
from pathlib import Path

DESCRIPTION = "Record a new long-running/multi-step task by overwriting memory/tasks/current_tasks.txt, for your own continuity across turns. Only call this for genuinely long-running or multi-step work you'll need to resume later -- NOT for a quick direct answer, which this channel's contract only allows one send() for per request (calling this tool does not itself send anything to the user; use send() for that). Required fields: person, requestchannel, taskcontent, completionsendcriterium, originalUserMessage."

def run(person, requestchannel, taskcontent, completionsendcriterium, originalUserMessage=""):
    """
    person: str - who requested the task (e.g. "Patrick Hammer")
    requestchannel: str - channel the request came from (e.g. "mattermost")
    taskcontent: str - description of the task to perform
    completionsendcriterium: str - criteria that determine when the task is complete and a send-back is warranted
    originalUserMessage: str - the original user message from which the task was inferred (default "")
    Overwrites memory/tasks/current_tasks.txt with the new task.
    Does not send anything to the user -- use send() for that.
    returns: str - success message
    """
    ROOT = Path(__file__).resolve().parent.parent
    TASKS_DIR = ROOT / "memory" / "tasks"
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    TASK_FILE = TASKS_DIR / "current_tasks.txt"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    content = f"""# Current Task (started {now})
# =============================================
# Person:               {person}
# Request Channel:      {requestchannel}
# Task Content:         {taskcontent}
# Completion Criterion: {completionsendcriterium}
# Original User Message: {originalUserMessage}
# =============================================
"""
    TASK_FILE.write_text(content)

    # NOTE (2026-09-15): deliberately does NOT send a channel notification.
    # This channel's contract (src/iter_outer_channel_protocosmo2.py) allows
    # exactly one send() per request -- it moves the request from processing/
    # to completed/ on first use. Auto-sending a "task started" notification
    # here consumed that one-shot slot, so the model's real final answer could
    # never be delivered (confirmed: it silently failed with
    # ChannelContractError, and the model just kept working indefinitely
    # trying to recover). Use the send tool explicitly for anything the user
    # should actually see.

    return f"SUCCESS, RETURN: New task recorded at {now}.\n  Person: {person}\n  Channel: {requestchannel}\n  Task: {taskcontent}\n  Completion criterion: {completionsendcriterium}\n  Original message: {originalUserMessage}\n  Written to: {TASK_FILE}\n  (no channel notification sent -- use send() for that)"
