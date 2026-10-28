"""t-2026-10-27-protomega-shared: the transport takes the bot identity only from
ITER_OUTER_IDENTITY and refuses to import without it (no default bot name)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IMPORT = "import src.iter_outer_channel_protocosmo2 as t; print(t._identity())"


def _run(identity):
    env = {k: v for k, v in os.environ.items() if k != "ITER_OUTER_IDENTITY"}
    env["ITER_OUTER_CHANNEL_ROOT"] = tempfile.mkdtemp()
    if identity is not None:
        env["ITER_OUTER_IDENTITY"] = identity
    return subprocess.run([sys.executable, "-c", IMPORT], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=30)


class TransportIdentityTest(unittest.TestCase):
    def test_identity_comes_from_variable(self):
        for name in ("ExampleBotA", "ExampleBotB"):
            r = _run(name)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), name)

    def test_missing_variable_fails_at_import(self):
        for value in (None, ""):
            r = _run(value)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("ITER_OUTER_IDENTITY is required", r.stderr)
            self.assertNotIn("ProtoMegaBot2", r.stdout)

    def test_no_default_bot_name_in_transport(self):
        src = (REPO / "src" / "iter_outer_channel_protocosmo2.py").read_text()
        self.assertNotIn("DEFAULT_IDENTITY", src)
        self.assertNotIn("ProtoMegaBot2", src)


if __name__ == "__main__":
    unittest.main()
