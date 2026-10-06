"""Multi-branch: draining pending derives while writers append must lose nothing."""
import importlib.util, json, multiprocessing, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _writer(path, i, n):
    import fcntl
    for k in range(n):
        entry = json.dumps({"note": f"w{i}-{k} " + "x" * 1500, "source_ids": []})
        with open(path, "a") as f:  # same protocol as tools/wmtm_derive.py
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(entry + chr(10)); f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def test_drain_concurrent_with_writers_loses_nothing(tmp_path):
    ctx_mod = _load("wmtm_context_t", "transformations/wmtm_context.py")
    p = tmp_path / "_wmtm_pending_derives.jsonl"; p.write_text("")
    mp = multiprocessing.get_context("fork")
    ws = [mp.Process(target=_writer, args=(str(p), i, 100)) for i in range(4)]
    [w.start() for w in ws]
    seen = []
    while any(w.is_alive() for w in ws):
        seen += ctx_mod._drain_pending_derives(p)
    [w.join(60) for w in ws]
    assert all(w.exitcode == 0 for w in ws)
    seen += ctx_mod._drain_pending_derives(p)
    notes = [json.loads(l)["note"] for l in seen if l.strip()]
    assert len(notes) == 400
    assert len(set(notes)) == 400
    assert p.read_text() == ""
