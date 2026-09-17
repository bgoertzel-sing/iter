# WMTM — Working Medium-Term Memory

A bounded-capacity mutable memory layer for cognitive agents, sitting between short-term memory (STM) and long-term memory (LTM). Implements ECAN-style attention allocation, PLN-based inference, GoalChainer decision integration, utility tracking, forgetting, and writeback to LTM.

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

### Cognitive Cycle (Orchestrator)

```
recall → attention → inference(basic) → PLN(step 2b) → GoalChainer(step 2c) → admit → contradictions → forget → writeback
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
| `wmtm/pln.py` | PLN atoms, TruthValue, deduction/induction inference rules |
| `wmtm/pln_bridge.py` | Converts WMTM items ↔ PLN atoms, runs PLN inference over working memory |
| `wmtm/goalchainer_bridge.py` | Converts WMTM evidence → GoalChainer input, parses decisions → InferenceCandidates |
| `wmtm/live_tool_bridge.py` | Parses real goalchainer_decide/wmtm_derive tool output → WMTM candidates |

### Supporting Files

| File | Purpose |
|---|---|
| `_petta_journal.py` | PeTTa journal parser/writer for LTM persistence |
| `benchmark_wmtm.py` | Performance benchmarks and parameter sweeps |
| `simulate_incident.py` | 8-phase incident response simulation (uses real GoalChainer output) |
| `test_*.py` | 393 tests across 27 files (100% module coverage) |

## Quick Start

```python
from wmtm import WMTMStore, WMTMOrchestrator

store = WMTMStore(capacity=100)
orch = WMTMOrchestrator(store=store, use_pln=True, use_goalchainer=True)

# Admit items
store.admit("i1", "dog is a animal", initial_sti=5.0)
store.admit("i2", "animal is a organism", initial_sti=3.0)

# Run a cognitive cycle — basic + PLN + GoalChainer fire simultaneously
result = orch.cycle()
print(f"Candidates: {len(result.candidates)}, Evicted: {len(result.evicted)}")
```

## Benchmark Results

- **Throughput:** ~107 cycles/sec
- **Stability:** 0 violations over 200 stress cycles
- **Inference:** 6/6 PASS (deduction, induction, abduction, analogy, evidence, contradiction)
- **PLN Bridge:** 13 tests — WMTM→PLN atom extraction, truth propagation, candidate conversion
- **GoalChainer Bridge:** 11 tests — evidence conversion, decision parsing, STI boosts/penalties
- **Live Tool Bridge:** 15 tests — real goalchainer_decide + wmtm_derive output parsing
- **Simulation:** 8-phase cognitive cycle with real GoalChainer integration
- **Tests:** 393/393 pass (27 files, `--timeout=15`)

## Design Principles

1. **Bounded capacity** — the store has a hard limit; old/low-attention items are evicted
2. **Attention-based** — ECAN-style STI/LTI values drive activation and forgetting
3. **Utility-aware** — items that are frequently used get higher utility scores
4. **Forgetting-aware** — a forgetting log prevents re-derivation of deliberately forgotten items
5. **Multi-engine inference** — basic inference + PLN + GoalChainer fire in each cycle
6. **Writeback** — high-value derived items are promoted to LTM for persistence
7. **Live integration** — real tool bridges parse actual goalchainer_decide/wmtm_derive output

## Running Tests

```bash
python3 -m pytest -q --timeout=15
```

## Running Benchmarks

```bash
python3 benchmark_wmtm.py
```

## Running Simulation

```bash
python3 simulate_incident.py
```
