# WMTM — Working Medium-Term Memory

A bounded-capacity mutable memory layer sitting between short-term context (STM)
and the append-only PeTTa long-term journal (LTM).  WMTM implements ECAN-style
attention allocation, PLN-based inference, utility tracking, forgetting, and
periodic writeback to LTM.

## Architecture

```
  ┌─────────┐    recall    ┌──────┐   infer    ┌───────────┐
  │ LTM     │ ───────────→ │ WMTM │ ─────────→ │ Derived   │
  │ Journal │ ←─────────── │ Store│            │ Candidates│
  └─────────┘   writeback  └──────┘            └───────────┘
                    ↑  forget ↓                    ↓ admit
                 ┌─────────┐                 ┌──────────┐
                 │Utility  │                 │Forgetting│
                 │Tracker  │                 │Log       │
                 └─────────┘                 └──────────┘
```

### Modules

| Module            | Responsibility                                          |
|-------------------|---------------------------------------------------------|
| `item.py`         | `WMTMItem` dataclass (id, content, source, attention)  |
| `attention.py`    | `AttentionValue` (STI/LTI) with exponential decay        |
| `store.py`        | `WMTMStore` bounded-capacity store with capacity FIFO    |
| `forgetting.py`   | `ForgettingPolicy` — eviction based on attention+utility|
| `inference.py`    | `WMTMInferenceEngine` — deduction, induction, abduction,|
|                   | analogy, evidence aggregation, contradiction detection  |
| `utility.py`      | `UtilityTracker` — tracks use/miss/inferred-from counts  |
| `forgetting_log.py`| `ForgettingLog` — prevents re-derivation of forgotten   |
| `writeback.py`    | `WritebackManager` — promotes high-utility items to LTM  |
| `orchestrator.py` | `WMTMOrchestrator` — ties all phases into one cycle      |
| `recall_bridge.py`| `RecallBridge` — LTM journal → WMTM keyword matching     |

### Orchestration Cycle

1. **Recall** — pull relevant items from LTM journal into WMTM
2. **Infer** — run inference engine over active set (deduction, induction, …)
3. **Admit** — admit novel, non-forgotten derived candidates
4. **Tick** — decay attention, age items
5. **Utility** — record use/miss for each item
6. **Forget** — evict low-attention/low-utility items (logged to prevent re-derivation)
7. **Writeback** — persist high-utility items back to LTM journal

## Quick Start

```python
from wmtm import WMTMStore, WMTMOrchestrator

store = WMTMStore(capacity=50)
orch = WMTMOrchestrator(store)

# Recall some items into the store
def recall_fn():
    return [("r1", "fire implies smoke", 80.0),
            ("r2", "smoke implies danger", 70.0)]

result = orch.cycle(recall_fn=recall_fn)
print(f"Admitted: {result.admitted_derived}, Active: {result.active_count}")
```

## Benchmark

```bash
python3 benchmark_wmtm.py
```

| Benchmark | Metric                          | Result             |
|-----------|---------------------------------|--------------------|
| B1        | Throughput                      | ~107 cycles/s      |
| B2        | Occupancy                       | Stable ≤ capacity  |
| B3        | Stability (200 stress cycles)   | 0 violations       |
| B4        | Inference (6 patterns)          | 6/6 PASS           |
| B5        | Writeback                       | PASS               |

## Testing

```bash
python3 -m pytest -q              # 285 tests, ~3.5s
python3 -m pytest -q --tb=short   # with tracebacks
python3 -m pytest -k inference    # specific module
```

## Code Quality

- **Docstring coverage**: 100% across all public functions
- **Return type annotations**: 100% across all public functions
- **Cyclomatic complexity**: All public functions ≤ 6 (remaining ≥7 are inherent algorithm logic)
- **0 TODO/FIXME/HACK** in live code
- **0 unused imports**

## Project Structure

```
.
├── iter.py                  # Iter agent runtime
├── _petta_journal.py        # PeTTa journal plumbing (append-only, supersession-resolved reads)
├── benchmark_wmtm.py        # Performance benchmarks & ECAN parameter sweep
├── wmtm/
│   ├── __init__.py          # Public API exports
│   ├── item.py              # WMTMItem
│   ├── attention.py         # AttentionValue (ECAN)
│   ├── store.py             # WMTMStore
│   ├── forgetting.py        # ForgettingPolicy
│   ├── forgetting_log.py    # ForgettingLog
│   ├── inference.py         # WMTMInferenceEngine (PLN)
│   ├── utility.py           # UtilityTracker
│   ├── writeback.py         # WritebackManager
│   ├── orchestrator.py      # WMTMOrchestrator
│   └── recall_bridge.py     # RecallBridge (LTM → WMTM)
├── test_*.py                # 19 test files, 285 tests
├── tools/                   # Iter agent tool plugins
├── channels/                # Iter agent communication channels
├── transformations/         # Iter agent belief transformations
└── memory/
    ├── _journal/            # PeTTa long-term journal
    └── *.txt                # Agent memory files
```
