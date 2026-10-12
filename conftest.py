"""Test-wide isolation: never let tests write the real memory/_journal/journal.metta.

_petta_journal._journal_dir() reads JOURNAL_DIR on every call, so pointing it at a
per-test tmp dir is enough. Subprocesses inherit it via os.environ.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_journal_dir(tmp_path, monkeypatch):
    jdir = tmp_path / "_journal"
    jdir.mkdir()
    monkeypatch.setenv("JOURNAL_DIR", str(jdir))
    yield
