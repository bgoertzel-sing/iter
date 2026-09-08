"""WMTM Performance Benchmark & ECAN Parameter Tuning.

Usage: python3 benchmark_wmtm.py
"""
from __future__ import annotations
import random, time
from wmtm import (
    WMTMStore, WMTMOrchestrator, ForgettingPolicy,
    UtilityTracker, WritebackManager,
)
from wmtm.attention import AttentionValue

SUBJECTS = ['alpha','beta','gamma','delta','epsilon','zeta','eta','theta','iota','kappa']
RELATIONS = ['implies','is-a','has','causes','prevents','precedes','enables']
OBJECTS = ['thing','concept','result','state','property','event','entity','process']


def make_recall(n, cycle, prefix='r'):
    return [
        (f'{prefix}{cycle}_{i}',
         f'{random.choice(SUBJECTS)} {random.choice(RELATIONS)} {random.choice(OBJECTS)}',
         random.uniform(30, 100))
        for i in range(n)
    ]


def benchmark_throughput():
    store = WMTMStore(capacity=200)
    orch = WMTMOrchestrator(store)
    start = time.time()
    for cycle in range(50):
        recalled = make_recall(10, cycle)
        orch.cycle(recall_fn=lambda r=recalled: r)
    elapsed = time.time() - start
    return {
        'cycles': 50, 'elapsed': elapsed, 'cycles_per_sec': 50 / elapsed,
        'final_store': len(store), 'forget_log': len(orch.forget_log),
    }


def benchmark_occupancy(capacity=50, cycles=50):
    random.seed(42)
    store = WMTMStore(capacity=capacity)
    orch = WMTMOrchestrator(store)
    occ, inf, ev = [], [], []
    for cycle in range(cycles):
        if cycle % 10 == 0:
            recalled = make_recall(8, cycle)
            r = orch.cycle(recall_fn=lambda r=recalled: r)
        else:
            r = orch.cycle()
        occ.append(len(store)); inf.append(r.admitted_derived); ev.append(r.evicted)
    return {
        'capacity': capacity, 'avg_occ': sum(occ) / len(occ),
        'max_occ': max(occ), 'min_occ': min(occ),
        'util_pct': sum(occ) / len(occ) / capacity * 100,
        'derived': sum(inf), 'evicted': sum(ev), 'stable': max(occ) <= capacity,
    }


def benchmark_stability(capacity=50, cycles=200):
    random.seed(123)
    store = WMTMStore(capacity=capacity)
    orch = WMTMOrchestrator(store)
    violations = 0
    for cycle in range(cycles):
        recalled = make_recall(random.randint(0, 15), cycle)
        orch.cycle(recall_fn=lambda r=recalled: r)
        if len(store) > capacity: violations += 1
    return {'cycles': cycles, 'capacity': capacity, 'violations': violations}


def benchmark_inference_patterns():
    results = {}
    # Deduction
    store = WMTMStore(capacity=50); orch = WMTMOrchestrator(store)
    r = orch.cycle(recall_fn=lambda: [('r1','alpha implies beta',80.0),('r2','beta implies gamma',80.0)])
    derived = [i for i in store.get_active_set() if i.source_type == 'derived']
    results['deduction'] = {'derived': r.admitted_derived, 'items': len(derived)}
    # Abduction
    store2 = WMTMStore(capacity=50); orch2 = WMTMOrchestrator(store2)
    r2 = orch2.cycle(recall_fn=lambda: [('r1','rain implies wet_ground',80.0),('r2','wet_ground is observed',90.0)])
    derived2 = [i for i in store2.get_active_set() if i.source_type == 'derived']
    results['abduction'] = {'derived': r2.admitted_derived, 'items': len(derived2)}
    # Induction
    store3 = WMTMStore(capacity=50); orch3 = WMTMOrchestrator(store3)
    r3 = orch3.cycle(recall_fn=lambda: [('r1','dog has fur',70.0),('r2','cat has fur',70.0),('r3','mouse has fur',70.0)])
    derived3 = [i for i in store3.get_active_set() if i.source_type == 'derived']
    results['induction'] = {'derived': r3.admitted_derived, 'items': len(derived3)}
    # Analogy
    store4 = WMTMStore(capacity=50); orch4 = WMTMOrchestrator(store4)
    r4 = orch4.cycle(recall_fn=lambda: [('r1','human has heart',80.0),('r2','dog has tail',80.0),('r3','human is-a mammal',80.0),('r4','dog is-a mammal',80.0)])
    derived4 = [i for i in store4.get_active_set() if i.source_type == 'derived']
    results['analogy'] = {'derived': r4.admitted_derived, 'items': len(derived4)}
    # Evidence aggregation
    store5 = WMTMStore(capacity=50); orch5 = WMTMOrchestrator(store5)
    r5 = orch5.cycle(recall_fn=lambda: [('r1','alpha implies beta',80.0),('r2','alpha implies beta',70.0),('r3','alpha implies beta',60.0)])
    derived5 = [i for i in store5.get_active_set() if i.source_type == 'derived']
    results['evidence_aggr'] = {'derived': r5.admitted_derived, 'items': len(derived5)}
    # Contradiction
    store6 = WMTMStore(capacity=50); orch6 = WMTMOrchestrator(store6)
    r6 = orch6.cycle(recall_fn=lambda: [('r1','sky is-a blue',80.0),('r2','sky is-a green',80.0)])
    contra_count = len(r6.contradictions) if r6.contradictions else 0
    results['contradiction'] = {'derived': r6.admitted_derived, 'items': contra_count}
    return results


def benchmark_writeback():
    store = WMTMStore(capacity=50)
    ut = UtilityTracker()
    wb = WritebackManager(min_age=2, min_utility=0.1, derived_min_age=1, derived_min_utility=0.1)
    orch = WMTMOrchestrator(store, utility_tracker=ut, writeback_manager=wb)
    wb_log = []
    recalled = [('r1','alpha implies beta',80.0),('r2','beta implies gamma',80.0)]
    for c in range(5):
        if c == 0:
            orch.cycle(recall_fn=lambda r=recalled: r, append_fn=lambda x: wb_log.append(x))
        else:
            orch.cycle(append_fn=lambda x: wb_log.append(x))
        for item in store.get_active_set()[:3]:
            store.touch(item.id); ut.record_use(item.id, c)
    return {'writebacks': len(wb_log)}


def benchmark_forgetting_log():
    store = WMTMStore(capacity=10); orch = WMTMOrchestrator(store)
    recalled = [(f'r{i}', f'item_{i} implies val_{i}', 50.0) for i in range(15)]
    r = orch.cycle(recall_fn=lambda r=recalled: r)
    return {'store_size': len(store), 'forget_log': len(orch.forget_log), 'evicted': r.evicted}


def parameter_sweep():
    """Sweep ECAN parameters at instance level (dataclass fields)."""
    configs = [
        ('baseline',     0.90, 0.97, 0.995, 0.05, 20),
        ('fast_decay',   0.80, 0.95, 0.99,  0.05, 20),
        ('slow_decay',   0.97, 0.99, 0.999, 0.05, 20),
        ('high_thresh',  0.90, 0.97, 0.995, 0.15, 20),
        ('low_thresh',   0.90, 0.97, 0.995, 0.02, 20),
        ('short_lived',  0.90, 0.97, 0.995, 0.05, 5),
        ('long_lived',   0.90, 0.97, 0.995, 0.05, 40),
        ('aggressive',   0.80, 0.95, 0.99,  0.15, 5),
        ('conservative', 0.97, 0.99, 0.999, 0.02, 40),
        ('balanced',     0.85, 0.96, 0.997, 0.08, 15),
    ]
    results = []
    for label, sd, ad, ld, st, dma in configs:
        random.seed(42)
        store = WMTMStore(capacity=50)
        orch = WMTMOrchestrator(store)
        orch.forgetting_policy = ForgettingPolicy(
            sti_threshold=st, derived_max_age=dma, derived_sti_threshold=st * 2)
        orig_admit = store.admit
        def patched(*a, **kw):
            item = orig_admit(*a, **kw)
            if item is not None:
                item.attention.sti_decay = sd
                item.attention.ati_decay = ad
                item.attention.lti_decay = ld
            return item
        store.admit = patched
        occ, inf, ev = [], [], []
        for cycle in range(100):
            if cycle % 10 == 0:
                recalled = make_recall(8, cycle)
                r = orch.cycle(recall_fn=lambda r=recalled: r)
            else:
                r = orch.cycle()
            occ.append(len(store)); inf.append(r.admitted_derived); ev
    print(f"Contradictions: {o["total_contradictions"]}")
    print()
    print("--- Stability (cap=50, 500 stress cycles) ---")
    s = benchmark_stability(capacity=50, cycles=500)
    print(f"Cycles: {s["cycles"]}, Capacity: {s["capacity"]}, Violations: {s["violations"]}")
    print()
    print("--- ECAN Parameter Sweep ---")
    print(f"{"Config":<16} {"AvgOcc":>7} {"Stable":>7} {"Deriv":>6} {"Evict":>6} {"Turn%":>6} {"Score":>6}")
    print("-" * 60)
    best = None
    for r in parameter_sweep():
        stab = "PASS" if r["stability"] else "FAIL"
        print(f"{r["label"]:<16} {r["avg_occ"]:>7.1f} {stab:>7} {r["derived"]:>6} {r["evicted"]:>6} {r["turnover"]*100:>5.1f}% {r["score"]:>6.3f}")
        if best is None or r["score"] > best["score"]:
            best = r
    print()
    print(f"Best config: {best["label"]} (score={best["score"]:.3f})")
        ('low_thresh',   0.90, 0.97, 0.995, 0.02, 20),
        ('short_lived',  0.90, 0.97, 0.995, 0.05, 10),
        ('long_lived',   0.90, 0.97, 0.995, 0.05, 40),
        ('aggressive',   0.85, 0.95, 0.99,  0.10, 10),
        ('conservative', 0.95, 0.98, 0.999, 0.02, 40),
        ('balanced',     0.92, 0.97, 0.997, 0.07, 15),
    ]

    print(f"\nB8 ECAN Parameter Sweep (200c/config, cap=50):")
    print(f"{'Config':<16}{'AvgOcc':>7}{'Stab':>5}{'Deriv':>6}"
          f"{'Evict':>6}{'Turn%':>6}{'Score':>6}")
    print('-' * 52)

    best = None
    for label, sd, ad, ld, st, dma in configs:
        random.seed(42)
        store = WMTMStore(capacity=50)
        orch = WMTMOrchestrator(store)
        orch.forgetting_policy = ForgettingPolicy(
            sti_threshold=st, derived_max_age=dma,
            derived_sti_threshold=st * 2,
        )
        # Patch admit to set decay on each new item
        orig_admit = store.admit
        def patched(*a, **kw):
            item = orig_admit(*a, **kw)
            if item is not None:
                item.attention.sti_decay = sd
                item.attention.ati_decay = ad
                item.attention.lti_decay = ld
            return item
        store.admit = patched

        occ_log, inf_log, evict_log = [], [], []
        for cycle in range(200):
            if cycle % 5 == 0:
                recalled = make_recall(5, cycle)
                r = orch.cycle(recall_fn=lambda r=recalled: r)
            else:
                r = orch.cycle()
            occ_log.append(len(store))
            inf_log.append(r.admitted_derived)
            evict_log.append(r.evicted)

        avg_occ = sum(occ_log) / len(occ_log)
        stability = max(occ_log) <= 50
        total_admitted = sum(inf_log) + sum(
            1 for c in range(0, 200, 5) for _ in range(5))
        turnover = sum(evict_log) / max(total_admitted, 1)

        if not stability:
            score = 0.0
        else:
            occ_s = max(0, 1.0 - abs(avg_occ / 50 - 0.7))
            turn_s = max(0, 1.0 - abs(turnover - 0.4))
            deriv_s = min(sum(inf_log) / 50, 1.0)
            score = occ_s * 0.4 + turn_s * 0.3 + deriv_s * 0.3

        stab_str = 'PASS' if stability else 'FAIL'
        print(f"{label:<16}{avg_occ:>7.1f}{stab_str:>5}"
              f"{sum(inf_log):>6}{sum(evict_log):>6}"
              f"{turnover*100:>5.1f}%{score:>6.3f}")

        if best is None or score > best[1]:
            best = (label, score, avg_occ, turnover, sum(inf_log))

    print(f"\nBest: {best[0]} (score={best[1]:.3f}, "
          f"occ={best[2]:.1f}, turnover={best[3]*100:.1f}%, "
          f"derived={best[4]})")


if __name__ == '__main__':
    print('=' * 60)
    print('WMTM Performance Benchmark')
    print('=' * 60)

    print()
    benchmark_throughput()

    print()
    benchmark_occupancy()

    print()
    benchmark_stability()

    print()
    print('--- Inference Pattern Coverage ---')
    benchmark_inference_patterns()

    print()
    benchmark_contradiction()

    print()
    benchmark_writeback()

    print()
    benchmark_forgetting_log()

    parameter_sweep()

    print()
    print('=' * 60)
    print('Benchmark complete.')
    print('=' * 60)

def benchmark_throughput():
    store = WMTMStore(capacity=200)
    orch = WMTMOrchestrator(store)
    start = time.time()
    for cycle in range(200):
        recalled = make_recall(10, cycle)
        orch.cycle(recall_fn=lambda r=recalled: r)
    elapsed = time.time() - start
    return dict(cycles=200, elapsed=elapsed, cycles_per_sec=200/elapsed, final_store=len(store), forget_log=len(orch.forget_log))


def benchmark_occupancy(capacity=50, cycles=200):
    random.seed(42)
    store = WMTMStore(capacity=capacity)
    ut = UtilityTracker()
    wb = WritebackManager(min_age=5, min_utility=1.0)
    orch = WMTMOrchestrator(store, utility_tracker=ut, writeback_manager=wb)
    occ_log, inf_log, evict_log, wb_log, contra_log = [], [], [], [], []
    for cycle in range(cycles):
        if cycle % 10 == 0:
            recalled = make_recall(8, cycle)
            r = orch.cycle(recall_fn=lambda r=recalled: r, append_fn=lambda c: wb_log.append(c))
        else:
            r = orch.cycle(append_fn=lambda c: wb_log.append(c))
        occ_log.append(len(store))
        inf_log.append(r.admitted_derived)
        evict_log.append(r.evicted)
        contra_log.append(len(r.contradictions) if r.contradictions else 0)
    return dict(capacity=capacity, avg_occupancy=sum(occ_log)/len(occ_log), max_occupancy=max(occ_log), min_occupancy=min(occ_log), utilization_pct=(sum(occ_log)/len(occ_log))/capacity*100, total_derived=sum(inf_log), total_evicted=sum(evict_log), total_writebacks=len(wb_log), total_contradictions=sum(contra_log), stability=max(occ_log)<=capacity)


def benchmark_stability(capacity=50, cycles=500):
    random.seed(123)
    store = WMTMStore(capacity=capacity)
    orch = WMTMOrchestrator(store)
    violations = 0
    for cycle in range(cycles):
        n = random.randint(0, 15)
        recalled = make_recall(n, cycle)
        orch.cycle(recall_fn=lambda r=recalled: r)
        if len(store) > capacity:
            violations += 1
    return dict(cycles=cycles, capacity=capacity, violations=violations)


def parameter_sweep():
    """Sweep key ECAN parameters and score configurations."""
    configs = [
        ('baseline',    0.90, 0.05, 20),
        ('fast_decay',  0.80, 0.05, 20),
        ('slow_decay',   0.97, 0.05, 20),
        ('high_thresh',  0.90, 0.15, 20),
        ('low_thresh',   0.90, 0.02, 20),
        ('short_lived',  0.90, 0.05, 10),
        ('long_lived',   0.90, 0.05, 40),
        ('aggressive',   0.80, 0.15, 10),
        ('conservative', 0.97, 0.02, 40),
        ('balanced',     0.92, 0.07, 15),
    ]
    results = []
    for label, sd, st, dma in configs:
        random.seed(42)
        store = WMTMStore(capacity=50)
        orch = WMTMOrchestrator(store)
        orch.forgetting_policy = ForgettingPolicy(sti_threshold=st, derived_max_age=dma, derived_sti_threshold=st*2)
        orig_admit = store.admit
        def make_patched(sd_val=sd):
            def inner(*args, **kwargs):
                item = orig_admit(*args, **kwargs)
                item.attention.sti_decay = sd_val
                return item
            return inner
        store.admit = make_patched()
        occ_log, inf_log, evict_log = [], [], []
        for cycle in range(200):
            if cycle % 5 == 0:
                recalled = make_recall(5, cycle)
                r = orch.cycle(recall_fn=lambda r=recalled: r)
            else:
                r = orch.cycle()
            occ_log.append(len(store))
            inf_log.append(r.admitted_derived)
            evict_log.append(r.evicted)
        avg_occ = sum(occ_log) / len(occ_log)
        stability = max(occ_log) <= 50
        total_admitted = sum(inf_log) + sum(1 for c in range(0, 200, 5) for _ in range(5))
        turnover = sum(evict_log) / max(total_admitted, 1)
        if not stability:
            score = 0.0
        else:
            occ_score = max(0, 1.0 - abs(avg_occ / 50 - 0.7))
            turn_score = max(0, 1.0 - abs(turnover - 0.4))
            deriv_score = min(sum(inf_log) / 50, 1.0)
            score = occ_score * 0.4 + turn_score * 0.3 + deriv_score * 0.3
        results.append(dict(label=label, avg_occ=avg_occ, stability=stability, derived=sum(inf_log), evicted=sum(evict_log), turnover=turnover, score=score))
    return results


if __name__ == "__main__":
    print('=' * 60)
    print('WMTM Performance Benchmark')
    print('=' * 60)
    print()
    print('--- Throughput ---')
    t = benchmark_throughput()
    print(f"Cycles: {t['cycles']}, Time: {t['elapsed']:.3f}s, Rate: {t['cycles_per_sec']:.0f} cycles/sec")
    print(f"Final store: {t['final_store']}, Forget log: {t['forget_log']}")
    print()
    print('--- Occupancy & Inference Rates (cap=50, 200 cycles) ---')
    o = benchmark_occupancy(capacity=50, cycles=200)
    print(f"Avg occupancy: {o['avg_occupancy']:.1f} / {o['capacity']} ({o['utilization_pct']:.1f}%)")
    print(f"Max: {o['max_occupancy']}, Min: {o['min_occupancy']}, Stable: {o['stability']}")
    print(f"Derived: {o['total_derived']}, Evicted: {o['total_evicted']}, Writebacks: {o['total_writebacks']}")
    print(f"Contradictions: {o['total_contradictions']}")
    print()
    print('--- Stability (cap=50, 500 stress cycles) ---')
    s = benchmark_stability(capacity=50, cycles=500)
    print(f"Cycles: {s['cycles']}, Capacity: {s['capacity']}, Violations: {s['violations']}")
    print()
    print('--- ECAN Parameter Sweep ---')
    print(f"{'Config':<16} {'AvgOcc':>7} {'Stable':>7} {'Deriv':>6} {'Evict':>6} {'Turn%':>6} {'Score':>6}")
    print('-' * 60)
    best = None
    for r in parameter_sweep():
        stab = 'PASS' if r['stability'] else 'FAIL'
        print(f"{r['label']:<16} {r['avg_occ']:>7.1f} {stab:>7} {r['derived']:>6} {r['evicted']:>6} {r['turnover']*100:>5.1f}% {r['score']:>6.3f}")
        if best is None or r['score'] > best['score']:
            best = r
    print()
    print(f"Best config: {best['label']} (score={best['score']:.3f})")
