# ProtoCosmo2 Iter concurrency: port reference branch

- Date: 2026-10-12
- Repo: `/home/openclaw/research-agent/projects/omegaclaw/protocosmo2/iter-port/repos` (fork `bgoertzel-sing/iter`). The live checkout was not modified: no checkout, stash, reset or commit. After the work, live `iter.py`, `channels/protocosmo2.py` and `tools/send.py` were still byte-identical to `fd065a3`.
- Worktree: `/tmp/claude-1001/-home-openclaw/cb18a9ee-4e7f-4b55-a3d2-4d0a25ab7645/scratchpad/concurrency-ref`
- Branch: `agent/protocosmo2-concurrency-port-reference`. It is local only and was not pushed.
- Purpose: reference material for re-implementing the concurrency work on the VM2 Protomega/Protomega2 Iter fork. It is not meant to be deployed.

## Base: `891fb58` (direct parent of M1 `8aceac0`)

- `891fb58:iter.py` is byte-identical to upstream `origin/master` (`f4064d9`, patham9/iter). `891fb58` only adds petta-memory files and does not touch `iter.py`. So `git diff 891fb58..branch -- iter.py` is also the diff against upstream Iter, which is the closest common ancestor a different fork is likely to share.
- The base is pre-M1, not pre-M2 (`c26da12`), because M1 (the Tier-1 checkpoint) is a dependency of the concurrency code, not a separate feature:
  - M3 step 3.5 (R20) queues `extract_tier1_checkpoint()` data from branches. The main thread then calls `write_checkpoint_file()` and `send_checkpoint_message()` in `drain_merge_queue()`.
  - The progress-not-pause stop path (foreground and branch) uses the same functions.
  - Leaving M1 in the base would hide code the port needs. It sits in its own first commit, so a porter can skip it if the target fork already has an equivalent.

## Commits (base..branch)

| # | Commit | Content | Source |
|---|---|---|---|
| 1 | `08170f4` | M1 Tier-1 checkpoint (`iter.py`, `.gitignore`) | tree == `c26da12` (squash of 8aceac0, 2f79cab, c26da12) |
| 2 | `2fa62cc` | M2+M3 core: threaded call with promotion deadline, BranchState, merge queue + double drain (R15), tags/supersede (R16), deadline abandon (R13), error markers (R14), shutdown (R17), branch mini-loop + checkpoint queuing (R20) | tree == `23a5d6f` (squash of 5fee852..23a5d6f) |
| 3 | `a920f40` | **Context, not concurrency.** `channels/protocosmo2.py` and `tools/send.py` as of fork master `87374c3`, so that later commits show only the hooks | 635a186, 3ebcbe2, dc1d872 |
| 4 | `06722dc` | Single-branch hardening + multi-send request lifecycle (Sep 22–30): branch gate, abandon-stops-work, branch error handoff, idle deadline from `last_progress_at`, `close_unfinished_requests()` (expire_stale/close_active), `send(final=)`, multi-send channel hooks | `347d849:iter.py` with exclusions; send/channel from `347d849` (fda9ac6, 61225e5, 5fcd01a, branch-gate-backoff 09-23, idle-loop 09-27) |
| 5 | `862ed7b` | Multi-branch: `ITER_MAX_BACKGROUND_BRANCHES`, request binding (`ITER_REQUEST_BINDING`, `_foreground_request_ids`, `_tool_context` → `ITER_REQUEST_IDS`), `receive_request`/`resume_all`/`active_request_ids` hooks, `ITER_MAX_CONCURRENT_LLM_CALLS` semaphore, `_bg_llm_create` in-flight marker + bounded bg retry | `3c86713:iter.py` with exclusions; send/channel from `3c86713` (f1cb71d, bc0ac63, 3c86713) |
| 6 | `a90978d` | Structured channel-hook results (`_STRUCTURED_RESULT_FUNCTIONS`), thread visibility (sibling view, branch inbox), branch text-only fallback, PROGRESS_NOT_PAUSE (per-request StepGuard, forked into branches, interim `final=false` notes, runaway backstops) | `fd065a3:iter.py` (== live) with exclusions + model tag removed (1ce3652, 7dcbae7) |
| 7 | `357bf5e` | 12 test files + `conftest.py`, `progress_harness.py`, `progress_guard_testlib.py`, verbatim from `fd065a3` | fd065a3 |
| 8 | `5c57559` | Adapts 7 model-tag assertions in 2 test files. Each edit is marked `REFERENCE BRANCH: model tag excluded` | n/a |

All commits are authored by Benjamin Goertzel <ben@singularitynet.io> and carry the Co-Authored-By / Claude-Session trailer. Every commit's `iter.py` passes `py_compile`.

The excluded features in commits 4–6 were removed mechanically by `scratchpad/strip_nonconc.py`, which logs the per-rule replacement counts. All rules matched exactly once per applicable site. A leftover check for `attach_turn_pdf`, `PDF_TURN`, `terminal_provider`, `turn_checkpoint`, `_tracked_llm_create`, `served_model`, `model_tag` and `MODEL_TAG` finds nothing.

## Files on the branch (vs base)

`iter.py` (+1444/−9), `channels/protocosmo2.py` (new, 28 lines; == live), `tools/send.py` (== live), `.gitignore` (+1, M1), and tests: `test_background_idle_deadline.py`, `test_bg_llm_hang.py`, `test_branch_gate_backoff.py`, `test_close_unfinished.py`, `test_invoke_structured_channel_results.py`, `test_llm_concurrency_cap.py`, `test_multi_branch.py`, `test_progress_not_pause.py`, `test_send_final.py`, `test_thread_visibility.py`, `test_text_only_fallback.py`, `test_text_only_retry_cap.py` (the last two cover entangled code), plus `conftest.py`, `progress_harness.py`, `progress_guard_testlib.py`.

## Excluded (separable), by feature

| Feature | Origin | What was removed |
|---|---|---|
| PDF turn images | backup `pre-pdf-turn-images-20260929T1925Z` | `# BEGIN/END PDF_TURN_IMAGES` block + `attach_turn_pdf_images()` call |
| MAX_TOKENS 2524→16000→32000 | `pre-max-tokens*` | reverted to 2524 |
| tool_choice required→auto (09-22)→required (10-05) | `pre-tool-choice-*` | net zero in live; commits 4–5 restore `"required"` (3 sites) |
| Model tag | c234e28 | MODEL_TAG block; `_tracked_llm_create` → `client.chat.completions.create` (2 sites); `model_tag` params of `send_checkpoint_message`/`send_text_only_fallback`/`send_progress_note`; tag args at 9 call sites; `_model_tag` in branch checkpoint payload |
| Terminal provider breaker | 2727dbe, 87374c3 | `terminal_provider_status`, `send_terminal_provider_failure`, `turn_checkpoint`, the except-handler block (including its `provider_idle = True`) |
| Docstrings on non-concurrency functions | dc1d872 | 10 one-line docstrings. The `BranchState.__init__` docstring is kept |
| `invoke_dynamic(path, /, ...)` | 957a469 | `/` removed |
| `DYNAMIC_TIMEOUT` 5→30 | 1519bf1 | reverted to 5 |
| prompt.txt changes | various | prompt.txt is untouched (base version) |
| WMTM / petta journal | f5a822c…, `_petta_journal.py` in fd065a3 | not present. Live `iter.py` has no WMTM/journal hooks |
| WMTM pending-derives flock | 18980d3, 7423976, 3156cde (multi-branch branch) | not included: `tools/wmtm_derive.py`/`transformations/wmtm_context.py` do not exist at this base. It is concurrency-relevant only if the target fork has WMTM derive tools that branches can run in parallel |
| `_channel_has` callable probe | 6ea3dd6 | never deployed (live uses a source-grep probe). Not included |
| `test_model_tag.py`, `test_terminal_provider_circuit.py` | | tests of excluded features |

## Entangled: kept and flagged (not concurrency by themselves)

1. **Provider retry backoff / quota idle** (branch-gate-backoff change, 2026-09-23). This covers `retryable_provider_error`, `provider_quota_exhausted`, `retry_after_seconds`, `retry_backoff_delay`, `backoff_wait`, `send_provider_exhausted_notice`, the `retryable_failures/capped_waits/provider_idle` state, and the except-handler backoff. It is kept because:
   - `provider_idle` shares the branch-gate `elif` in the main loop.
   - Branch errors are re-raised into this handler.
   - `_bg_llm_create` retries depend on the classifier. `test_bg_llm_hang` and `test_branch_gate_backoff` test it.
   - `import re` (originally from 2727dbe) is kept because the backoff code uses it.
2. **Idle pacing and text-only handling.** This covers the `AUTONOMOUS_CHECKIN_INTERVAL` throttle (09-22), the idle-loop nop fix (09-27), and `TEXT_ONLY_RETRY_CAP` + main-loop text-only idle/fallback with `send_text_only_fallback`/`unwrap_pseudo_send`. It is kept because:
   - The branch loop calls `send_text_only_fallback()`.
   - Progress-not-pause sets `_last_autonomous_ts`, and its stop path depends on the throttled post-task mode.
   - The text-only idle block resets the StepGuard.
3. **M1 checkpoint format change** in `format_checkpoint_message` (`[PAUSED] … stop_reason`). This is part of progress-not-pause and is kept.

## Behavioural differences vs live that a porter should know

- **Terminal 4xx errors.** With the terminal breaker excluded, a terminal 4xx from the foreground or a branch goes through the generic non-retryable path: sleep `ERROR_RECOVERY_TIME`, then the next loop. Live instead sends a notice and idles (`provider_idle = True`). This except-handler shape never ran live.
- **`DYNAMIC_TIMEOUT`.** It is 5 s at this base and 30 s live. The request-binding channel hooks (`receive_request`, `active_request_ids`, `close_active`, `expire_stale`) run in `invoke_dynamic` subprocesses, so choose the timeout deliberately.
- **Outer channel contract.** The contract the hooks call is **outside this repo**: `protocosmo2/iter-port/src/iter_outer_channel_protocosmo2.py`, plus `iter_producer`/`iter_responder`/`iter_relay`. It provides `receive_request`, `resume_all`, `active_request_ids`, `close_active`, `expire_stale` and `send(content, final=, request_id=)`. The target fork needs an equivalent channel contract.
- **Prompt instruction.** Live prompt.txt has a PROGRESS_NOT_PAUSE instruction (send `final=false` interim notes) that was excluded here. The `tools/send.py` DESCRIPTION documents `final=`.

## Test results

Command set (12 files): `python3 -m pytest -q -p no:cacheprovider test_background_idle_deadline.py test_bg_llm_hang.py test_branch_gate_backoff.py test_close_unfinished.py test_invoke_structured_channel_results.py test_llm_concurrency_cap.py test_multi_branch.py test_progress_not_pause.py test_send_final.py test_text_only_fallback.py test_text_only_retry_cap.py test_thread_visibility.py`

| Run | Result |
|---|---|
| `python3 -m py_compile iter.py` in worktree (branch HEAD and each commit) | OK |
| Branch HEAD, directly in the worktree | **87 passed, 18 failed, 1 skipped**. The 18 failures are all in `test_progress_not_pause.py` (MainLoopScenarios, ChannelDeliveryTest, ReceiverWiringTest). They need `../src/iter_outer_channel_protocosmo2.py` etc. and `../../tools/phase6_private_canary_runner.py` from the ProtoCosmo2 project layout. |
| Unmodified `fd065a3` tree (git archive) in a non-project scratch dir | **87 passed, the same 18 failed, 1 skipped**. This shows the 18 failures are environmental, not caused by the exclusions. |
| Staged layout (`scratchpad/run_tests.sh`: copy of repo + copy of `iter-port/src/*.py` + read-only symlink to the runner), unmodified `fd065a3` | **105 passed, 1 skipped** (baseline) |
| Staged layout, branch with verbatim live tests (before commit 8) | **98 passed, 7 failed, 1 skipped**. All 7 assert model-tag behaviour: `test_non_closing_send_with_model_tag`, `test_refused_note_reported` (loads `apply_model_tag`), `test_branch_runs_past_old_budget_with_interim_notes` (expects tag), and in `test_text_only_fallback.py`: `test_fallback_model_tag`, `test_main_loop_wires_fallback_only_for_unanswered_user_requests`, `test_fallback_unwraps_send_written_as_text`, `test_branch_loop_delivers_unanswered_text_only_ending` |
| Staged layout, branch HEAD `5c57559` (tests adapted) | **105 passed, 1 skipped** |

The 1 skip appears in every run: `test_multi_branch.py::RealChannelTest` needs the sibling worktree `../wt-multibranch-channel`.

## Diff vs live `iter.py`

`git diff 891fb58..agent/protocosmo2-concurrency-port-reference -- iter.py` (+1444/−9) is the concurrency diff, including M1 and the entangled items above.

The residual `git diff agent/protocosmo2-concurrency-port-reference fd065a3 -- iter.py` (+320/−19, 28 hunks; saved as `scratchpad/concurrency-ref-residual-vs-live.diff`) contains **only** the excluded features:
- PDF turn-images block + call
- MODEL_TAG block + `_tracked_llm_create` + tag arguments/params
- terminal breaker functions + except block + `turn_checkpoint`
- `MAX_TOKENS` 2524→32000
- `DYNAMIC_TIMEOUT` 5→30
- `invoke_dynamic` `/`
- the 10 dc1d872 docstrings

`tool_choice` is identical to live. `channels/protocosmo2.py` and `tools/send.py` on the branch are byte-identical to live.

## Reproduce / provenance

- Strip script: `scratchpad/strip_nonconc.py IN OUT [--model-tag]`. Inputs: `347d849:iter.py`, `3c86713:iter.py`, `fd065a3:iter.py --model-tag`. Snapshots are in `scratchpad/snap/`.
- Test runner: `scratchpad/run_tests.sh <repo-dir> <label>`
- Lineage evidence:
  - `347d849:iter.py` == `iter.py.pre-multibranch-20261001T020052Z`
  - `1ce3652:iter.py` == `iter.py.pre-progress-not-pause-20261005T0256Z`
  - `7dcbae7` → live differs only by tool_choice auto→required (3 lines)
  - The backup chain from `87374c3` to live is contiguous: `87374c3` == `pre-tool-choice-auto`. Most consecutive backup diffs are a single named change; the exception is model-tag + multi-branch, which share one interval.
  - Inferred, not verified: the plain `pre-thread-visibility` backup appears to be post-change, because it is identical in size and mtime to `pre-progress-not-pause`. The `.reconstructed` file looks like the real pre-state.
