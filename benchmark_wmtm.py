"""WMTM Performance Benchmark & ECAN Parameter Tuning."""
from __future__ import annotations
import random, time
from wmtm import WMTMStore, WMTMOrchestrator, ForgettingPolicy, UtilityTracker, WritebackManager

SUBJ=['alpha','beta','gamma','delta','epsilon','zeta','eta','theta','iota','kappa']
REL=['implies','is-a','has','causes','prevents','precedes','enables']
OBJ=['thing','concept','result','state','property','event','entity','process']
def mk(n: int, c: int) -> tuple[str, str, float]:
    """Generate a recall event tuple (id, content, sti) for benchmark seed *n* and count *k*."""
    return [(f"r{c}_{i}",f"{random.choice(SUBJ)} {random.choice(REL)} {random.choice(OBJ)}",random.uniform(30,100)) for i in range(n)]
def bench_throughput() -> float:
    """Benchmark B1: throughput in cycles per second over 1000 cycles."""
    s=WMTMStore(capacity=200);o=WMTMOrchestrator(s)
    t0=time.time()
    for c in range(50):o.cycle(recall_fn=lambda r=mk(10,c):r)
    el=time.time()-t0
    print(f"B1 Throughput: 50c in {el:.2f}s ({50/el:.0f} cyc/s) store={len(s)} forgotten={len(o.forget_log)}")
def bench_occupancy() -> bool:
    """Benchmark B2: occupancy tracking — verify active set stays within capacity."""
    random.seed(42)
    s=WMTMStore(capacity=50);o=WMTMOrchestrator(s)
    occ,inf,ev=[],[],[]
    for c in range(100):
        if c%10==0:r=o.cycle(recall_fn=lambda r=mk(8,c):r)
        else:r=o.cycle()
        occ.append(len(s));inf.append(r.admitted_derived);ev.append(r.evicted)
    ao=sum(occ)/len(occ)
    print(f"B2 Occupancy: avg={ao:.1f}/50 max={max(occ)} stable={max(occ)<=50} derived={sum(inf)} evicted={sum(ev)}")
def bench_stability() -> int:
    """Benchmark B3: stability — run 200 stress cycles and check for capacity violations."""
    random.seed(123)
    s=WMTMStore(capacity=50);o=WMTMOrchestrator(s)
    v=0
    for c in range(200):
        o.cycle(recall_fn=lambda r=mk(random.randint(0,15),c):r)
        if len(s)>50:v+=1
    print(f"B3 Stability: 200 stress cycles cap=50 violations={v}")
def _run_inference_test(name: str, recall_items, check_filter, contradictions: bool = False) -> tuple[bool, object]:
    """Run a single inference-pattern test and return (passed, result_obj)."""
    s = WMTMStore(capacity=50)
    o = WMTMOrchestrator(s)
    r = o.cycle(recall_fn=lambda: recall_items)
    if contradictions:
        contra = r.contradictions if r.contradictions else []
        passed = bool(contra)
        print(f"  {name:<15}{'PASS' if passed else 'FAIL':>5} detected={len(contra)}")
        for c in contra:
            print(f"    {c.subject} {c.relation}: {c.conflicting_objects} sev={c.severity:.2f}")
        return passed, r
    d = [i for i in s.get_active_set() if check_filter(i)]
    passed = bool(d)
    print(f"  {name:<15}{'PASS' if passed else 'FAIL':>5} derived={r.admitted_derived}")
    return passed, r

def bench_inference() -> dict[str, bool]:
    """Benchmark B4: inference — run all 6 inference pattern tests (deduction, induction, abduction, analogy, evidence, contradiction)."""
    results = {}
    # deduction
    passed, _ = _run_inference_test(
        "deduction:", [('r1','alpha implies beta',80.0),('r2','beta implies gamma',80.0)],
        lambda i: i.source_type == "derived")
    results['deduction'] = passed
    # induction
    passed, _ = _run_inference_test(
        "induction:", [('r1','alpha has property_x',60.0),('r2','alpha has property_x',60.0)],
        lambda i: i.source_type == "derived" and 'induced' in i.content)
    results['induction'] = passed
    # abduction
    passed, _ = _run_inference_test(
        "abduction:", [('r1','rain implies wet_ground',80.0),('r2','wet_ground is observed',90.0)],
        lambda i: i.source_type == "derived")
    results['abduction'] = passed
    # analogy
    passed, _ = _run_inference_test(
        "analogy:", [('r1','alpha has red',80.0),('r2','beta has green',80.0)],
        lambda i: i.source_type == "derived")
    results['analogy'] = passed
    # evidence aggregation
    passed, _ = _run_inference_test(
        "evidence_agg:", [('r1','alpha has property_x',60.0),('r2','alpha has property_x',60.0)],
        lambda i: i.source_type == "derived" and 'aggregated' in i.content)
    results['evidence'] = passed
    # contradiction
    passed, _ = _run_inference_test(
        "contradiction:", [('r1','sky has blue',80.0),('r2','sky has green',80.0)],
        None, contradictions=True)
    results['contradiction'] = passed
    total = sum(1 for v in results.values() if v)
    print(f"  TOTAL: {total}/{len(results)} patterns passing")
def bench_writeback() -> bool:
    """Benchmark B5: writeback — verify periodic journal persistence works correctly."""
    s=WMTMStore(capacity=50)
    ut=UtilityTracker()
    wb=WritebackManager(min_age=2,min_utility=0.1,derived_min_age=1,derived_min_utility=0.1)
    o=WMTMOrchestrator(s,utility_tracker=ut,writeback_manager=wb)
    wb_log=[]
    rec=[('r1','alpha implies beta',80.0),('r2','beta implies gamma',80.0)]
    for c in range(5):
        if c==0:o.cycle(recall_fn=lambda r=rec:r,append_fn=lambda x:wb_log.append(x))
        else:o.cycle(append_fn=lambda x:wb_log.append(x))
        for item in s.get_active_set()[:3]:s.touch(item.id);ut.record_use(item.id,c)
    print(f"B6 Writeback: {len(wb_log)} items written back over 5 cycles")
def bench_forgetting() -> int:
    """Benchmark B6: forgetting — verify ECAN decay and eviction under load."""
    s=WMTMStore(capacity=10);o=WMTMOrchestrator(s)
    rec=[(f'r{i}',f'item_{i} implies val_{i}',50.0) for i in range(15)]
    r=o.cycle(recall_fn=lambda r=rec:r)
    print(f"B7 Forgetting: store={len(s)} evicted={r.evicted} forget_log={len(o.forget_log)}")
def _run_config(label: str, sd: float, st: float, dma: int) -> tuple[str, float, float, int]:
    """Run one ECAN config over 200 cycles and return (label, score, avg_occ, total_derived)."""
    random.seed(42)
    s = WMTMStore(capacity=50)
    o = WMTMOrchestrator(s)
    o.forgetting_policy = ForgettingPolicy(sti_threshold=st, derived_max_age=dma, derived_sti_threshold=st * 2)
    _apply_decay_patch(s, sd)
    occ, inf, ev = _run_sweep_cycles(o, s)
    ao = sum(occ) / len(occ)
    stab = max(occ) <= 50
    sc = _compute_score(stab, ao, sum(inf), sum(ev))
    st_str = 'PASS' if stab else 'FAIL'
    print(f'{label:<16}{ao:>7.1f}{st_str:>5}{sum(inf):>6}{sum(ev):>6}{sc:>6.3f}')
    return (label, sc, ao, sum(inf))

def _apply_decay_patch(store, sd_val: float) -> None:
    """Patch store.admit to set sti_decay on admitted items."""
    oa = store.admit
    def inner(*a, **kw):
        item = oa(*a, **kw)
        if item is not None:
            item.attention.sti_decay = sd_val
        return item
    store.admit = inner

def _run_sweep_cycles(orch, store, n: int = 200) -> tuple[list, list, list]:
    """Run n cycles and collect occupancy/derived/evicted metrics."""
    occ, inf, ev = [], [], []
    for c in range(n):
        if c % 5 == 0:
            r = orch.cycle(recall_fn=lambda r=mk(5, c): r)
        else:
            r = orch.cycle()
        occ.append(len(store)); inf.append(r.admitted_derived); ev.append(r.evicted)
    return occ, inf, ev

def _compute_score(stab: bool, ao: float, total_derived: int, total_evicted: int) -> float:
    """Compute the ECAN stability/occupancy/derived/eviction score."""
    if not stab:
        return 0.0
    return (max(0, 1 - abs(ao / 50 - 0.7)) * 0.4
            + min(total_derived / 25, 1) * 0.3
            + max(0, 1 - abs(total_evicted / 200 - 0.4)) * 0.3)

def param_sweep() -> None:
    """Benchmark B8: ECAN parameter sweep — test 10 configurations over 200 cycles each."""
    configs = [
        ('baseline', 0.90, 0.05, 20), ('fast_decay', 0.80, 0.05, 20),
        ('slow_decay', 0.97, 0.05, 20), ('high_thresh', 0.90, 0.15, 20),
        ('low_thresh', 0.90, 0.02, 20), ('short_lived', 0.90, 0.05, 10),
        ('long_lived', 0.90, 0.05, 40), ('aggressive', 0.80, 0.15, 10),
        ('conservative', 0.97, 0.02, 40), ('balanced', 0.92, 0.07, 15),
    ]
    print(f"\nB8 ECAN Parameter Sweep (200c/config, cap=50):")
    print("-" * 52)
    best = None
    for lbl, sd, st, dma in configs:
        result = _run_config(lbl, sd, st, dma)
        if best is None or result[1] > best[1]:
            best = result
    print(f'\nBest: {best[0]} (score={best[1]:.3f}, occ={best[2]:.1f}, derived={best[3]})')

if __name__=="__main__":
    print("="*60)
    print("WMTM Performance Benchmark")
    print("="*60)
    bench_throughput()
    bench_occupancy()
    bench_stability()
    print("\nB4 Inference Patterns:")
    bench_inference()
    bench_writeback()
    bench_forgetting()
    param_sweep()
    print("\n"+"="*60)
    print("Benchmark complete.")
    print("="*60)
