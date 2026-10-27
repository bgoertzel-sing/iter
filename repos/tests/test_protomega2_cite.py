"""hive task t-2026-10-24-telegram-message-cite: the protomega2 channel must
put telegram:<source.chat_id>/<source.message_id> in front of every prompt.

Source ids below are made up (one private chat, one group);
no real ids or chat text are used.  Run: python3 -m unittest repos/tests/test_protomega2_cite.py
"""
import hashlib
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "repos"))
sys.path.insert(0, str(REPO))

PRIVATE = {"chat_id": 123456789, "message_id": 4242, "update_id": 700000001}
GROUP = {"chat_id": -1009876543210, "message_id": 777, "update_id": 700000002}


class CiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name)) / "chan"
        os.environ["ITER_OUTER_CHANNEL_ROOT"] = str(self.root)
        os.environ["ITER_OUTER_IDENTITY"] = "ProtoMegaBot2"
        os.environ.pop("ITER_MAX_OPEN_REQUESTS", None)
        self.impl = importlib.import_module("src.iter_outer_channel_protocosmo2")
        self.ch = importlib.import_module("channels.protomega2")
        for d in ("inbox", "processing"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _put(self, prompt, source):
        rid = hashlib.sha256((prompt + repr(source)).encode()).hexdigest()
        req = {"schema_version": self.impl.SCHEMA_VERSION, "identity": "ProtoMegaBot2",
               "request_id": rid, "prompt": prompt,
               "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "source": source}
        (self.root / "inbox" / (rid + ".json")).write_bytes(self.impl._canonical(req))
        return rid

    def test_private_and_group(self):
        for source in (PRIVATE, GROUP):
            with self.subTest(chat=source["chat_id"]):
                for p in (self.root / "processing").glob("*.json"):
                    p.unlink()
                rid = self._put("please do X", source)
                want = f"(cite as telegram:{source['chat_id']}/{source['message_id']}) please do X"
                got = self.ch.receive_request()
                self.assertEqual(got, {"request_id": rid, "prompt": want})
                self.assertEqual(self.ch.resume(), want)
                self.assertEqual(self.ch.resume_all(), [{"request_id": rid, "prompt": want}])

    def test_empty_inbox(self):
        self.assertIsNone(self.ch.receive_request())
        self.assertEqual(self.ch.receive(), "")

    def test_cite_rejects_bad_source(self):
        with self.assertRaises(self.impl.ChannelContractError):
            self.ch.cite({"chat_id": "1", "message_id": 2, "update_id": 3})


if __name__ == "__main__":
    unittest.main()
