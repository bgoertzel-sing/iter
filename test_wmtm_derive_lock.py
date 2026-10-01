"""Multi-branch: concurrent wmtm_derive appends must not lose or interleave lines."""
import importlib.util, json, multiprocessing, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load(tmp_root):
    tools = tmp_root / "tools"; tools.mkdir(exist_ok=True)
    (tmp_root / "memory").mkdir(exist_ok=True)
    src = (ROOT / "tools" / "wmtm_derive.py").read_text()
    (tools / "wmtm_derive.py").write_text(src)
    spec = importlib.util.spec_from_file_location("wmtm_derive_t", tools / "wmtm_derive.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m, tmp_root / "memory" / "_wmtm_pending_derives.jsonl"


def _proc_worker(root, i):
    m, _ = _load(Path(root))
    for k in range(50):
        m.run(f"p{i}-{k} " + "x" * 2000)


def test_threads_append_exactly(tmp_path):
    m, p = _load(tmp_path)
    ts = [threading.Thread(target=lambda i=i: [m.run(f"t{i}-{k}") for k in range(50)]) for i in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    lines = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert len(lines) == 200
    assert len({l["note"] for l in lines}) == 200


def test_processes_append_exactly(tmp_path):
    _, p = _load(tmp_path)
    ctx = multiprocessing.get_context("fork")
    ps = [ctx.Process(target=_proc_worker, args=(str(tmp_path), i)) for i in range(4)]
    [x.start() for x in ps]; [x.join(60) for x in ps]
    assert all(x.exitcode == 0 for x in ps)
    lines = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert len(lines) == 200
    assert len({l["note"] for l in lines}) == 200
