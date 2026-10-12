"""Thread visibility: branches see sibling tasks/sends and follow-ups that arrive while they run."""
import ast
import os
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = (HERE / "iter.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
NAMES = ("_snippet", "_inbound_snippet", "_note_send", "_note_inbound", "_register_branch_activity",
         "_mark_branch_activity_done", "take_branch_inbox", "sibling_view")


def load():
    ns = {"time": time, "threading": threading, "os": os, "SIBLING_SEND_WINDOW": 1800,
          "SIBLING_SNIPPET_CHARS": 300, "_sibling_lock": threading.Lock(), "_branch_activity": {},
          "_recent_sends": [], "_last_inbound": None}
    for name in NAMES:
        node = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(ast.get_source_segment(SRC, node), ns)
    return ns


class ThreadVisibilityTest(unittest.TestCase):
    def test_branch_sees_sibling_task_and_send_but_not_its_own(self):
        ns = load()
        ns["_note_inbound"]("[ctx] ... vocal cue options?")
        ns["_register_branch_activity"]("bg-a")
        ns["_note_inbound"]("[ctx] ... github daily push review")
        ns["_register_branch_activity"]("bg-b")
        ns["_note_send"]("bg-a", "Metronaut, Live Tempo Follower, Antescofo...")
        view_b = ns["sibling_view"]("bg-b")
        self.assertIn("running bg-a", view_b)
        self.assertIn("vocal cue", view_b)
        self.assertIn("sent by bg-a", view_b)
        self.assertNotIn("running bg-b", view_b)
        view_a = ns["sibling_view"]("bg-a")
        self.assertNotIn("sent by bg-a", view_a)
        self.assertIn("github daily push", view_a)

    def test_follow_up_reaches_running_branch_once(self):
        ns = load()
        ns["_note_inbound"]("first task")
        ns["_register_branch_activity"]("bg-a")
        self.assertEqual(ns["take_branch_inbox"]("bg-a"), "")  # its own request is not echoed back
        ns["_note_inbound"]("[ctx] something to keep in mind for later")
        inbox = ns["take_branch_inbox"]("bg-a")
        self.assertIn("keep in mind for later", inbox)
        self.assertIn("follow-up to YOUR task", inbox)
        self.assertEqual(ns["take_branch_inbox"]("bg-a"), "")  # drained

    def test_finished_branch_gets_no_inbox_and_drops_from_running(self):
        ns = load()
        ns["_note_inbound"]("task")
        ns["_register_branch_activity"]("bg-a")
        ns["_mark_branch_activity_done"]("bg-a")
        ns["_note_inbound"]("later")
        self.assertEqual(ns["take_branch_inbox"]("bg-a"), "")
        self.assertEqual(ns["sibling_view"](None), "")

    def test_foreground_view_and_send_window(self):
        ns = load()
        ns["_note_send"]("foreground", "own")
        self.assertEqual(ns["sibling_view"](None), "")  # foreground does not see its own sends
        ns["_recent_sends"].append((time.time() - 4000, "bg-x", "old"))
        self.assertNotIn("old", ns["sibling_view"]("bg-y"))
        self.assertIn("sent by foreground", ns["sibling_view"]("bg-y"))

    def test_unknown_branch_inbox_is_safe_and_snippets_bounded(self):
        ns = load()
        self.assertEqual(ns["take_branch_inbox"]("nope"), "")
        self.assertTrue(ns["_inbound_snippet"]("x" * 1000 + " end").endswith("end"))
        self.assertLessEqual(len(ns["_snippet"]("y" * 1000)), 301)

    def test_wiring_present_in_main_and_branch_loops(self):
        for needle in ("_register_branch_activity(branch_id)", "_mark_branch_activity_done(branch_id)",
                       "_note_inbound(event_append)", "sibling_view(None)", "take_branch_inbox(branch_id)",
                       "messages=call_messages", '_note_send("foreground"', "_note_send(branch_id"):
            self.assertIn(needle, SRC)


if __name__ == "__main__":
    unittest.main()
