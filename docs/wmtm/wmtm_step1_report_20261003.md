# WMTM step 1 (I0.1 + I0.2, read-only) — interim report, 2026-10-03 00:05 PDT

Approved by Ben in msg 12384 (reply to 12383). Nothing in Omega or the live system was changed.

## OCO/2 reference package (drafts/oco2_package_20261002, rebuilt from msg 12362 ZIP)
- unittest discover -s reference: 48 tests OK (0.53 s), jsonschema 4.26.0.
- reference/run_mutations.py: clean checks pass; all 7 mutants fail their intended assertions.
- Schema pin (sha256):
  - schema/oco2.schema.json      812d8917faf8d528941fa552971d6edfd9c881bbec8612bb94200b487281ea18
  - schema/oco2_registry.json    58982b7e5b0fdd672bfaf342b3edcb03e391512aa3782c95f1f8e2c17aafdd70
  - schema/profile_catalog.json  d3c2378d8114b35ad38f39100028d2bea359e3702974d3a364dcf4f1ccbf5c2d
- Caveat: rebuilt from extracted ZIP text, not original archive bytes.

## Source lock (local checkouts as found — NOT yet the live plan branches)
- OmegaClaw-Core: main d98ffee (2026-08-06), dirty: ?? knowledge-priors/, remote asi-alliance/OmegaClaw-Core
- iter (ProtoCosmo2 live): master 87374c3, 73 dirty files, remote patham9/iter (not erayindex)
- Hugo_folio: protomega/initial-review 249f896, clean, remote singnet/Hugo_folio
- OmegaClaw-GoalChainer: HEAD 10627ce

Findings:
- Local OmegaClaw-Core is ~2 months old; plan wants MeTTaClaw2 / iter erayindex / Max_folio erayindex. None checked out here. Next: read-only fetch and record heads.
- GoalChainer HEAD is 10627ce (branch agent/provenance-dedup-local). RESOLVED 00:02: d993e49 is real and older — "GGB Gate 1: goal/task adapter conformance validator + negative test matrix", 2026-07-14 — and is contained in main, agent/provenance-dedup-local and agent/provenance-dedup-for-patrick. So 'local at d993e49' is stale, not wrong: d993e49 is the Gate 1 commit; current local tip is 10627ce (heuristic PLN fallback + fact_name provenance). The 8/8 smoke result is tied to d993e49 and has not been re-run on 10627ce. Worktree provenance-dedup at 33f58a2.
- No Max_folio checkout found locally.

## Remote heads (git ls-remote, 2026-10-03 00:49 PDT, read-only)
- patham9/iter: master f4064d9 (HEAD), erayindex 59152a7. Local live iter is master 87374c3 -> behind/diverged from remote master.
- asi-alliance/OmegaClaw-Core: main 04dfa2e. Local checkout d98ffee (Aug 6) is stale; run.metta git-import! would pull 04dfa2e-era code.
- patham9/mettaclaw: main 7b30527, neoclaw 2af37c3. No MeTTaClaw2 branch name found in that repo; confirm where the plan's MeTTaClaw2 lives.
- Max_folio erayindex: not yet located.

## Remote heads (ls-remote 2026-10-03 00:49 PDT, read-only, nothing cloned)
- patham9/iter: master f4064d9 (= HEAD); erayindex 59152a7. Live local copy is 87374c3 on master + 73 dirty files, so it is behind remote master.
- asi-alliance/OmegaClaw-Core: main 04dfa2e (local checkout d98ffee, 2026-08-06, is behind).
- patham9/mettaclaw: main 7b30527; neoclaw 2af37c3. No branch named MeTTaClaw2 on this remote; confirm which repo/branch the plan means.
- Max_folio erayindex: not queried yet (repo URL unknown locally).
- Note: grep for def/class names with wmtm|supersed|recall|sti|consolid|decay|memory|promot in iter.py returned nothing; WMTM code lives under other names/files (_wmtm_state.json, benchmark_wmtm.py). Ledger must locate it first.

## I0.2 load trace (static, local d98ffee)
- run.metta: import lib_import -> git-import! OmegaClaw-Core (GitHub) -> import lib_omegaclaw -> !(omegaclaw).
  git-import! may load the remote HEAD rather than this checkout; confirm which copy PeTTa resolves.
- lib_omegaclaw.metta imports src/{helper.py,utils,config(.py),logger.py,log,plugin(.py),agentverse.py,channels(.py),providers(.py),fileio.py,skills,websearch.py,memory,context,loop,rag.py}, providers/lib_llm_ext.py, profile/policy, petta_lib_chromadb.
- CONFIRMED: ./src/context is imported but src/context.metta does not exist (all other targets exist). Open: hard fail vs silent skip in PeTTa; needs a marker run.

## Still to do
1. Read-only fetch of the plan branches; finalize source lock.
2. Marker trace of one ordinary turn, one swipl process at a time (petta-chem shards hold most of the 16 GB).
3. Reuse/port/replace ledger: current WMTM vs amended I1-I4 packets.
4. Native Episode A/B attempt with local swipl 9.3.36 + PeTTa.
5. Large-attachment receiver change (approved in 12384) — not started.


## Upstream heads (git ls-remote, 2026-10-03 00:49 PDT) + shallow clone 01:35
- patham9/iter: master f4064d9 (HEAD), erayindex 59152a7
- asi-alliance/OmegaClaw-Core: main 04dfa2e (HEAD) — cloned read-only to ~/research-agent/scratch/wmtm-step1-src/OmegaClaw-Core-04dfa2e
- patham9/mettaclaw: main 7b30527 (HEAD), neoclaw 2af37c3
- Upstream OmegaClaw-Core 04dfa2e: lib_omegaclaw.metta is gone; entry lib is now lib_omega.metta (plus lib_nal.metta, lib_pln.metta). New src files: embedding_models.py, memory_export.py.
- ./src/context still missing upstream: lib_omega.metta:26 imports (library Omega ./src/context), and no *context* file exists in the repo. So the gap is in current main, not just the stale Aug 6 local copy. Hard-fail vs silent-skip still needs the marker run.
- Local d98ffee (Aug 6) is behind upstream; the plan's trace must use 04dfa2e (or the MeTTaClaw2 branch, not yet located).


## I0.3 draft ledger: current WMTM vs OCO/2 packets (static read, 02:07 Oct 3; module docstrings only, not yet line-level)
| Current piece (iter-port/repos) | What it does | OCO/2 target | Verdict |
|---|---|---|---|
| _petta_journal.py | append-only PeTTa journal; supersession resolved in-memory per read | I1.2 exact inert content + I2 withdrawal events | PORT: append-only matches; Supersedes atoms -> typed withdrawal/supersession events with cutoff views |
| control/event_store.py + control/types.py | SQLite append-only event log, immutable frozen records, superseding corrections, revision checks (I3/I7/I8) | ledger/event backbone (I1/I2) | REUSE as pattern; records must be rebound to oco/2 kinds + content digests |
| wmtm/recall_bridge.py | lexical LTM->WMTM pull (stopword-filtered IDF) | I3 retrieval over admitted records | PORT: keep as candidate generator only; ranking must respect standing (fixes Sep 28 stale-over-new) |
| wmtm/store.py, attention.py, forgetting.py, utility.py | bounded mutable STI store, decay, eviction, use/miss | I3 single retention reducer (rational ticks, use_kinds, max_entries/bytes/tokens) | REPLACE with reducer; float STI -> rational credit; mutable store becomes derived view |
| wmtm/writeback.py | promotes high-utility items to LTM | I2 admission policy | REPLACE: promotion must become an admission event; model text only as attributed testimony |
| wmtm/inference.py, wmtm/pln.py | regex triples + PLN-style deduction/induction/abduction | OmegaPLN recipes/justifications (I4) | REPLACE: derived beliefs must carry justification + checker; current triples unchecked |
| wmtm/goal_store.py, goalchainer_bridge.py, live_tool_bridge.py | JSON goals; GoalChainer decisions -> WMTM candidates | I5 goals/verifier | DEFER to I5; GoalChainer d993e49 (8/8 smoke) vs local 10627ce (untested) |
| transformations/wmtm_context.py | singleton orchestrator, injects active set into prompt, writes _wmtm_state.json | context snapshot/ReadSet (I3/I4.5) | PORT: injection should cite snapshot id + ledger cutoff |

Open: line-level check of writeback + forgetting_log; confirm neoclaw src/memory.metta promotionInflationFactor/mostPromotedMemories overlap with the reducer row.


## 02:23 update (read-only, via GitHub API/raw)
Source lock additions:
- asi-alliance/Max_folio (public): erayindex a4453f3, main fc21db2, master 10e41bd, gh-pages 9ecbf20.
- "MeTTaClaw2": GitHub repo search returns 0 results. Not a public repo name; may be a branch, a private repo, or the plan's name for patham9/mettaclaw. Ask Ben which one.
- patham9/mettaclaw also has branches NAL 7d0c98e, NextVersion 7a03d2c, lib_pln a58d024, lib_nal_update a476023 (besides main 7b30527, neoclaw 2af37c3).

neoclaw src/memory.metta @2af37c3 vs planned single reducer (I3):
- Promotion = per-memory scalar in a Python-side map (helper.promotion_*), decayed by power law v*(1+dt/86400)^-0.7; promote = min(10, v+1), demote = max(0, v-1). It's mutable in-place state, not events.
- query: fetches promotionInflationFactor(10) x maxRecallItems(10) nearest neighbours, keeps only promotion>0, sorts by promotion (distance is the tiebreak), then appends the plain nearest neighbours.
- update = createMemory + forget_ids on the old ids (hard delete, promotion and STV copied over). There's no withdrawal record, so history of what was superseded is lost.
- tvUpdate/support/contradict revise the STV with Truth_Revision against stv(1|0, 0.5) and link the episode.
Verdict: PORT the arithmetic (decay curve, cap 10, revision) into the I3 reducer as an exact rational policy (OCO/2 retention_policy has decay_factor/max_credit/tick_ns). REPLACE the storage: in-place map writes and hard deletes become use/withdrawal events, so supersession stays auditable. This fixes the same class of bug as my Sep 28 stale-note ranking.


## I0.2 ./src/context: hard fail or silent skip? (static, 03:11 PDT Oct 3)
Source: local PeTTa repos/PeTTa @ 4ce1d0e (2026-07-06), src/metta.pl:283-296.
- 'import!'(Space, File, true) :- catch(importer_helper(Space, File), _, fail).
- For a .metta target, importer_helper does ensure_metta_ext + exists_file(PathWithExt), ! then load_metta_file.
- A missing ./src/context.metta makes exists_file fail -> importer_helper fails -> import! FAILS SILENTLY: no error and no output, and the load continues to ./src/loop.
- Consequence: anything that context.metta should define is undefined at runtime. Calls to those functions stay unevaluated and nothing reports the gap.
- Wider hazard: catch(_, _, fail) also swallows exceptions from files that do exist (parse/load errors, a missing Python module), so any broken import vanishes the same way.
- Caveat: this is the Jul 6 local PeTTa; the PeTTa that run.metta pulls via lib_import may differ. A marker run is still worth doing to see which symbols loop.metta expects from context, but no swipl run is needed to answer hard fail vs skip.
- Suggested I0 check: after the imports, assert that every expected import resolved, so the run fails loudly when one is missing.


## 03:15 Oct 3: what the loop needs from ./src/context (static, 04dfa2e)
- Every function src/loop.metta calls (getPrompt, getSkills, maxFeedback, getHistory, addToHistory, add-prompt-extension, heartbeat, last_chars, get_time_as_string, embeddingprovider, applySecurityPolicy, initMemory, commchannel, version) has a (= ...) definition in the other imported .metta files (src/*, lib_omega, profile/policy, lib_nal, lib_pln).
- So the ordinary turn does not depend on context.metta; its absence looks like a stale import (or a removed/renamed module), not a functional hole in the main loop. Not checked: plugins/ and python-side callers.
- I0 recommendation stands: fail loudly on missing imports, then delete or restore the context import deliberately.

## Large-attachment receiver change (Ben 12384) — staged, NOT live (2026-10-03 04:1x PDT)
- Receiver: tools/phase6_private_canary_runner.py (Api.extract_document). PDFs were already saved + capped at PDF_INLINE_MAX_CHARS_ITER=120k; ZIP/text were inlined whole (12362 = 923k chars -> blocked channel).
- Patch: drafts/large_attach_20261003/large_attach.patch (93 lines). New store_large_attachment(): if extracted text > inline limit (120k in --use-iter mode only), save original (.zip/.raw) + full text (.txt) mode 0600 under state_dir/attachments/<day>/, inline a receipt + bounded prefix. Retention prune now groups .zip/.raw too. Non-iter mode unchanged (limit 2M).
- Tests: drafts/large_attach_20261003/test_large_attachment.py, 7/7 pass on the copy. Rebuilt OCO/2 package ZIP: 98 KB zip, 897,604 chars extracted -> 120,000 inline, full text byte-identical on disk.
- Not done: install into live receiver + receiver restart (duplicate-supervisor risk on every restart; do it attended, when idle, with backup). Note: 120k + group context must stay < 200k; same budget PDFs already use.


## Episode A/B scoping (2026-10-03 04:40, read-only)
Source: notes/integration_plan_amendment.md "First native acceptance targets"; verification/coverage.json not_implemented.
- Episode A needs native recall -> checked derivation with pure guard -> host use -> retention -> restart -> evidence correction. Episode B needs qualified test result -> cognitive GoalEvaluation -> accepted satisfaction event, plus rejection cases.
- These exercise I1 codecs, I2 lifecycle/events, I3 reducer, I4 instrumented reasoner and I5 collectors. None exist yet; the amendment says it "supplies no fictional native API". The plan places running them under I6 acceptance.
- Correction to my 12383 suggestion: Episode A/B cannot be run in step 1. Having swipl/PeTTa here only removes the package's environment blocker, not the missing implementation.
- Step 1 substitute (optional, cheap): native smoke that the fixture native atoms in fixtures/native_content_examples.metta parse/load under local PeTTa 4ce1d0e. Gives an early parser datapoint for I1, not Episode evidence.
- Step 1 status: complete except that optional smoke; report ready for Ben's review.

## Amendment 2026-10-03 (after Ben's ECAN question, reply to 12412)
Ledger row "wmtm/store.py, attention.py, forgetting.py, utility.py -> REPLACE" means replacing my small Python STI store (no spreading, no Hebbian links, so not real ECAN). It does NOT mean dropping ECAN.
Split:
- Prompt-context retention, recency, dedupe, supersession: deterministic I3 reducer and lifecycle events (simple processes suffice).
- ECAN proper (STI/LTI, importance spreading over HebbianLinks, attentional focus): inference control over the growing Atomspace (premise/rule selection for PLN/chainer, rationing of reasoning work, forgetting at scale). Attention is non-semantic: it can never change truth, standing or admission. Its state is recorded via the OCO/2 attention anchors/leases/MemoryUse attention events.
- Forgetting = lease expiry / archive, not deletion, so provenance survives.
- Candidate source: iCog-Labs metta-attention (local, petta-memory/repos/metta-attention, attention-bank).
- Proposed packet: ECAN inference control after I4 (instrumented reasoner), measured against recency+similarity baseline on the same episode.
