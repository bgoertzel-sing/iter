# WMTM module audit (read-only), 2026-10-04 22:13–22:30 PDT

Requested by glicerico (Telegram, after msg 12437). No code changed. Only this report was written.

## Scope and state
- Repo: `iter-port/repos` (ProtoCosmo2 live iter), branch `master` @ `87374c3`, **81 dirty paths** (step-1 report said 73).
- `wmtm/`: 18 modules + `__init__.py`, 3,692 lines.
- **8 of the 18 modules have uncommitted edits** (410+/19−): forgetting, forgetting_log, inference, item, orchestrator, recall_bridge (+201), store, utility. These are the F01b/F08b/F15-era fixes. The code that runs and is tested here is **not in any commit**.
- Tests are at repo root (`test_*.py`), not in `tests/`.
- How callers were found: grep for `wmtm.<mod>` / relative imports in non-test .py files (worktrees `wt-*` excluded), plus reading the runtime entry points.

## Live runtime path (what actually runs)
- `transformations/wmtm_context.py` builds a singleton `WMTMOrchestrator(_store)` and pulls LTM via `recall_bridge.parse_journal` + `RecallBridge`. On real user turns (within the 15 s budget) it runs the full `cycle()` with `append_fn=_petta_journal.append_note`, so **writeback writes to the LTM journal today**. On automated turns it only does `store.tick()` + utility tick. It injects the active set into the prompt and saves `memory/_wmtm_state.json`.
- `tools/goalchainer_decide.py` also builds `WMTMOrchestrator(store)` and `RecallBridge`.
- Both use the defaults `use_pln=True` and **`use_goalchainer=False`**. So PLN runs live, but `goalchainer_bridge` is **not** on the live path.
- `control/wmtm_adapters.py`, the only non-test user of goal / goal_store / pln_bridge outside the orchestrator, is imported **only by tests** (test_wp8_integration, test_wp9_e2e, test_coverage_edge_cases).

## Test run
- 30 test files run one at a time: **all pass**, 480 tests, 0 failures (log: `/tmp/wmtm_audit_tests_20261005.txt`).
- The same set run together under pytest-cov: 482 passed in 14.5 s. **Line coverage of wmtm/ is 90%** (1,732 statements, 166 missed).

## Module by module
| Module | What it does | Callers (non-test) | Tests | Cov |
|---|---|---|---|---|
| __init__.py | Package facade; re-exports Store, Orchestrator, etc. | wmtm_context, goalchainer_decide | indirect (all) | 100% |
| attention.py | ECAN-style AttentionValue (STI→ATI→LTI with exponential decay; consolidation on boost()). No spreading, no Hebbian links. | item, store | test_wmtm_attention (21), test_wmtm, test_wmtm_item | 100% |
| item.py | WMTMItem dataclass: content, source_type recalled/derived, AV, utility, wall-clock age (uncommitted +30). | nearly all wmtm modules | test_wmtm_item (10), test_wmtm | 87% |
| store.py | WMTMStore: bounded mutable active set; admit/evict/touch/tick, lowest-STI eviction, pending-evicted drain. | orchestrator, recall_bridge, inference, writeback, utility, forgetting | test_wmtm_store (27) + many | 89% |
| utility.py | UtilityTracker: use/miss/inferred-from counts → utility score; LTM promotion candidates. | orchestrator | test_wmtm_utility (15) | 99% |
| forgetting.py | ForgettingPolicy: evicts on low STI, aged low utility, wall-clock stale (F15: >6 h and zero utility), over capacity. | orchestrator | **no dedicated file**; test_wmtm only | 94% |
| forgetting_log.py | Append-only log of evicted items (content hash + id) to block re-derivation; FIFO cap (F08b), to/from_dict (F01b). | orchestrator | test_wmtm_forgetting_log (13) | 94% |
| recall_bridge.py | LTM→WMTM pull: two-pass journal parse that drops superseded clusters, scoring by keyword IDF + bigrams + token-level spreading activation over the active set. Biggest uncommitted delta (+201). | wmtm_context, goalchainer_decide, wmtm_eval | test_recall_bridge (32), test_wmtm_recall_bridge (23), test_wmtm_recall (15) | 89% |
| embed_index.py | F34 sentence-embedding (bge-small) index + RRF fusion for hybrid recall; degrades to keyword-only. | **only wmtm_eval/stage1_replay.py** (eval) | **NONE** | **0%** |
| inference.py | WMTMInferenceEngine: regex triples (->, "is a", "has"); deduction/induction/abduction/analogy, evidence aggregation, contradiction detect+resolve. | orchestrator, pln_bridge, goalchainer_bridge | test_wmtm_inference (35), test_wmtm_contradiction_resolution (15) | 97% |
| pln.py | PLN TruthValue algebra + PLNInferenceEngine rules; its own regex atom extractor (duplicates inference.py's patterns; has/have). | pln_bridge | test_pln (38) | 97% |
| pln_bridge.py | WMTM items ↔ PLN atoms (F04: uses stored TV); runs PLN over active set; maps TV back to attention. | orchestrator (live, use_pln=True), control/wmtm_adapters | test_pln_bridge (13), test_pln_orchestrator (8) | 93% |
| writeback.py | Selects high-utility derived/recalled items, formats MeTTa MemoryCluster, appends via petta_append (F03 checks {"ok"}). | orchestrator (**live writes to journal**) | test_wmtm_writeback (10) | 96% |
| orchestrator.py | WMTMOrchestrator.cycle(): recall → infer → PLN → (GoalChainer) → admit → tick → utility → forget → writeback; snapshot/restore. | wmtm_context, goalchainer_decide | test_wmtm_orchestrator (10) + goal/pln/goalchainer orchestrator tests | 97% |
| goal.py | Goal dataclass with lifecycle (activate/achieve/abandon/block, expiry). | goal_store, control/wmtm_adapters (test-only) | test_goal (35) | 98% |
| goal_store.py | JSON-file GoalStore; queries by status/priority, subgoal links. | orchestrator (optional goal_store, not passed live) | test_goal, test_goal_orchestrator (11) | 100% |
| goalchainer_bridge.py | WMTM active set → GoalChainer evidence → decisions → InferenceCandidates. | orchestrator (gated, **off live**), live_tool_bridge, control/wmtm_adapters | test_goalchainer_bridge (11), test_goalchainer_orchestrator (19) | 94% |
| live_tool_bridge.py | Parses goalchainer_decide / wmtm_derive tool text; LiveGoalChainerAdapter. | **only simulate_incident.py** | test_live_tool_bridge (15) | 98% |

### Coverage flags
- **No test coverage: embed_index.py (0%, no test file).** It is also not on the live path.
- **No dedicated test file: forgetting.py.** It's 94% covered indirectly by test_wmtm.py. I didn't check whether the 4 missed lines are the F15 wall-clock branch.
- Lowest of the rest: item 87%, store 89%, recall_bridge 89% (30 missed lines, in the largest uncommitted change).
- Test-only / dead in production: live_tool_bridge, goal, goal_store, goalchainer_bridge (gated off), embed_index (eval only).

## Differences vs the drafts
drafts/wmtm_assessment_ben_20261002.md (A) and drafts/wmtm_step1_report_20261003.md (S1, I0.3 ledger + ECAN amendment):

1. **Ledger coverage.** S1 covers 11 of the 18 modules. It leaves out item, orchestrator, pln_bridge, goal, embed_index and forgetting_log; forgetting_log is listed as "open". Suggested verdicts: item/orchestrator → REPLACE along with the I3 reducer; pln_bridge → REPLACE with I4; forgetting_log → REPLACE with withdrawal/lease events; embed_index → drop or park (see 3).
2. **recall_bridge is more than "lexical IDF".** S1 calls it a "lexical LTM→WMTM pull (stopword-filtered IDF)". The code also scores bigrams and runs token-level spreading activation, and parse_journal already drops superseded clusters (two-pass, the 891fb58 ghost fix). The ECAN amendment's "no spreading" is therefore slightly wrong: there is spreading, but over shared tokens, not HebbianLinks. The "not real ECAN" conclusion still holds. The remaining Sep 28 gap is ranking of stale notes that have **no** Supersedes marker.
3. **embed_index is in neither draft.** The F34 vector/RRF index is in wmtm/ but used only by the eval script, consistent with the Sep 27 "no vector/RRF" decision. The drafts should say it is parked so I3 doesn't count it as live retrieval.
4. **GoalChainer path is off in production.** S1 groups goal_store, goalchainer_bridge and live_tool_bridge as the GoalChainer path, deferred to I5. Both live entry points construct the orchestrator with use_goalchainer=False, live_tool_bridge has no production caller, and goal/goal_store are reached only through test-only control/wmtm_adapters. DEFER to I5 is fine, but these are currently dead code, not a live loop.
5. **Two inference engines run live.** S1 lumps inference.py and pln.py together. In practice the orchestrator runs both every user-turn cycle (inference.py, then PLN via pln_bridge with use_pln=True), and they use near-duplicate regex extractors. This adds weight to the I4 REPLACE verdict and should be noted for the native-cost measurement.
6. **Writeback is live.** S1 says REPLACE writeback with an admission event, but doesn't note that writeback **already writes model-derived beliefs to the LTM journal** on real user turns, with no attribution. That is the "model text becomes fact" risk from A, happening today, not hypothetically.
7. **Source state drifted.** S1 says 73 dirty files; there are now 81. Neither draft says that 8 wmtm modules (410 lines), including the fixes the tests rely on, are uncommitted. The I0 source lock should record this or commit it to a branch, with Ben's or glicerico's OK.
8. **Confirmed as stated.** A's "TOPIC_SUPERSEDE defined but unused" is true: it exists only in wmtm_eval/stage1_replay.py. S1's descriptions of attention, store, utility, forgetting, writeback and the regex triples match the code.

## Not done
- No line-level review of the missed-coverage lines.
- No test-coverage check of transformations/wmtm_context.py (outside wmtm/).
- No commit or other change of any kind.
