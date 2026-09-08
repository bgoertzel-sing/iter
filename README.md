# WMTM — Working Medium-Term Memory

A bounded-capacity mutable memory layer for cognitive agents, sitting between short-term memory (STM) and long-term memory (LTM). Implements ECAN-style attention allocation, PLN-based inference, utility tracking, forgetting, and writeback to LTM.

## Architecture

```
LTM Journal ──► RecallBridge ──► WMTMStore (bounded) ──► WritebackManager ──► LTM Journal
                     │                  │                     │
                     ▼                  ▼                     ▼
                LTMCluster      InferenceEngine        WritebackCandidate
                                 │
                                 ▼
                          ContradictionReport
```

### Core Modules

| Module | Purpose |
|---|---|
| `wmtm/item.py` | `WMTMItem` dataclass — items in the store |
| `wmtm/attention.py` | `AttentionValue` — STI/LTI with decay |
| `wmtm/store.py` | `WMTMStore` — bounded-capacity mutable store |
| `wmtm/forgetting.py` | `ForgettingPolicy` — eviction decisions |
| `wmtm/inference.py` | `WMTMInferenceEngine` — PLN deduction/induction/abduction/analogy |
| `wmtm/utility.py` | `UtilityTracker` — use/miss tracking |
| `wmtm/forgetting_log.py` | `ForgettingLog` — prevents re-derivation of forgotten items |
| `wmtm/writeback.py` | `WritebackManager` — promotes high-value items to LTM |
| `wmtm/orchestrator.py` | `WMTMOrchestrator` — ties all components into a single cycle |
| `wmtm/recall_bridge.py` | `RecallBridge` — bridges LTM journal clusters to WMTM |

### Supporting Files

| File | Purpose |
|---|---|
| `_petta_journal.py` | PeTTa journal parser/writer for LTM persistence |
| `benchmark_wmtm.py` | Performance benchmarks and parameter sweeps |
| `test_wmtm_*.py` | 285 unit tests (100% public function coverage) |

## Quick Start

```python
from wmtm import WMTMStore, WMTMOrchestrator

store = WMTMStore(capacity=100)
orch = WMTMOrchestrator(store=store)

# Admit an item
item = store.admit("i1", "fire implies smoke", initial_sti=5.0)

# Run a cognitive cycle
result = orch.cycle()
print(f"Candidates: {len(result.candidates)}, Evicted: {len(result.evicted)}")
```

## Benchmark Results

- **Throughput:** ~107 cycles/sec
- **Stability:** 0 violations over 200 stress cycles
- **Inference:** 6/6 PASS (deduction, induction, abduction, analogy, evidence, contradiction)
- **Tests:** 285/285 pass

## Design Principles

1. **Bounded capacity** — the store has a hard limit; old/low-attention items are evicted
2. **Attention-based** — ECAN-style STI/LTI values drive activation and forgetting
3. **Utility-aware** — items that are frequently used get higher utility scores
4. **Forgetting-aware** — a forgetting log prevents re-derivation of deliberately forgotten items
5. **Inference-driven** — PLN inference generates new candidates each cycle
6. **Writeback** — high-value derived items are promoted to LTM for persistence

## Running Tests

```bash
python3 -m pytest -q
```

## Running Benchmarks

```bash
python3 benchmark_wmtm.py
```
