# Iter concurrency: porting spec (ProtoCosmo2 → Protomega / Protomega2)

Written 2026-10-12. Source of truth: ProtoCosmo2's live Iter on Pop!_OS. Every claim
cites a file:line, a commit, a backup file or a record. Anything not checked first-hand
is tagged **[UNVERIFIED]**. Anything worked out by reading code (not seen in a test or a
log) is tagged **[CODE-READING]**.

Abbreviations used below:

- `REPO` = `~/research-agent/projects/omegaclaw/protocosmo2/iter-port/repos` (the Iter fork; remotes `fork`=bgoertzel-sing/iter, `origin`=patham9/iter)
- `PORT` = `~/research-agent/projects/omegaclaw/protocosmo2/iter-port` (transport repo; local-only git)
- `EXP` = `~/research-agent/projects/omegaclaw/experiments`
- `RUNNER` = `~/research-agent/projects/omegaclaw/protocosmo2/tools/phase6_private_canary_runner.py`

Line numbers for `iter.py` refer to the live file `REPO/iter.py`. It is byte-identical
to `git show fd065a3:iter.py` (checked with `cmp`), and fd065a3 is on branch
`agent/protocosmo2-checkout-sync-20261011`. The running process (PID 319538) started
Mon 2026-10-05 14:02:19 PDT. The file's mtime is 2026-10-05 11:04 PDT, so the process
runs the current file.

Companion: branch `agent/protocosmo2-concurrency-port-reference` (this branch) holds the
isolated diff; see `docs/concurrency-port/REFERENCE_BRANCH_REPORT.md`.

> **Update 2026-10-12 (after VM2 gate 1). Read this before the rest.**
> Five fixes were made on the laptop and applied to this branch. The sections below describe
> fd065a3 and are marked where a fix changes them. Line numbers below are fd065a3's; the
> fixed `iter.py` is shifted by about +30 lines after line 430.
>
> | Fix | What changed | Where described |
> |---|---|---|
> | F1 | `_channel_has` probes real callables (`__has_attrs__` in the dynamic worker, cached by mtime/size), as in 6ea3dd6. `close_unfinished_requests` uses it too. | §5.1, B5 |
> | F2 | **Promotion race fixed.** The call's outcome and `branch_id` are read and written under a per-call `promotion_lock`. `branch_id` is published last, after the branch client and messages. A call that finishes at the deadline is answered in the foreground (`LLM_PROMOTION_RACE_AVOIDED`). | §9.5 item 1 |
> | F3 | **Original errors kept (VM2 K9).** A call that fails within the deadline re-raises the original exception (type, `status_code`, `response` headers such as Retry-After), not `Exception("Background LLM call failed: …")`. | §2.2 |
> | F4 | **Binding whenever concurrency is on (VM2 D7).** `ITER_REQUEST_BINDING = ITER_CONCURRENCY_ENABLED`, so 1 branch means 2 open requests. | §2.3, §9.3 step 7 |
> | F5 | `tools/send_document.py` (laptop only) uses the request bound via `ITER_REQUEST_IDS`. | §9.5 item 2 |
> | F6 | **Exclusive final answers (VM2 D12).** Channel: the outbox entry is created exclusively (`os.link`), so a racing closer can no longer overwrite a real answer with a close note; `close_active` returns `False` when another writer won. Listing open requests tolerates one closing mid-listing. | §5.1 |
> | F7 | **Expiry spares owned requests (VM2 D11).** `close_unfinished_requests` passes `expire_stale(keep=...)` the foreground's and every active branch's request; a channel without `keep` skips expiry for that cycle (`STALE_REQUEST_EXPIRE_SKIPPED`). Before, a branch's request could get the timeout note and its real answer was refused. | §5.1, §7 item 14 |
> | F8 | **Roll-back keeps merged branch entries (VM2 D13).** Drain (b) merges after `history_checkpoint`; a failed call's roll-back now keeps those entries (`_rollback_keeping_merged`). | §7 item 14 |
> | F9 | **Arrival order.** The channel claims inbox requests by (mtime, name), not sha256 filename order. | §5.1 |
>
> Tests: `test_channel_has_probe.py` and `test_concurrency_fixes_20261012.py` (8 tests; 7 fail on
> fd065a3); `test_d11_d13_20261013.py` (6 tests; all fail before F7/F8). On this branch: 120 passed,
> 5 skipped in the ProtoCosmo2 layout. F6/F9 are channel changes (PORT 06f94d5, transport tests
> `test_iter_outer_channel_d11_d12.py`); the transport is not published, so port them from the
> description in §5.1.
> Laptop deploy: branch `agent/protocosmo2-concurrency-fixes-20261012` (F1–F5 9aadb38 on top of fd065a3,
> F7/F8 8071bed), deployed 2026-10-12/13.
> New: **§10 Routing under concurrency (hive-route)**.

---

## 1. Purpose and status

**What it does.** Iter's main loop is single-threaded: it receives, calls the model,
runs tools, and repeats. With concurrency on, every model call runs in a helper thread,
and the main thread waits for it for at most `ITER_PROMOTE_SECONDS` (30 s). If the call
finishes in time, the turn goes on exactly as before. If it does not, the call is
**promoted**: it becomes a *background branch* that finishes the call and then keeps
working on that task in its own mini-loop (model call → tools → model call …). It
answers its own request. Meanwhile the main thread goes back to polling for input,
so a new message gets a reply without waiting for the slow task. Up to
`ITER_MAX_BACKGROUND_BRANCHES` (3) branches run at the same time. Each one is bound to
the outer-channel request it was started from (multi-request channel, up to
`ITER_MAX_OPEN_REQUESTS` open requests).

**Live history (ProtoCosmo2):**

| Date (PDT) | Milestone | Evidence |
|---|---|---|
| 2026-09-04 | M2/M3 code (single branch, flag default OFF) | commits 5fee852..23a5d6f |
| 2026-09-06 15:56 | `ITER_CONCURRENCY_ENABLED=1` live, single branch | `protocosmo2/iter-concurrency/PLAN.md` step 4.2; `m4-final-report.md` |
| 2026-09-30 19:01 | Multi-branch (3 branches) + model tag live; channel stayed single-request (see §6 bug B5) | `EXP/20261001T0200Z-protocosmo2-model-tag/RESULT.md`; `TASKS.md` |
| 2026-10-01 10:38 | Multi-request channel live (`ITER_MAX_OPEN_REQUESTS=3`), request binding active end to end | `EXP/20261001T1735Z-protocosmo2-multirequest-channel/README.md` |
| 2026-10-04 20:18 | Progress-not-pause (branch 25-step stop removed) | `EXP/20261005T0256Z-protocosmo2-progress-not-pause/RUN.md` |
| 2026-10-05 14:02 | Current process; receiver timeout 1500 s + late-answer delivery | `EXP/20261005T2023Z-iter-long-request-audit/RUN.md` |

**Live flags** (from the brief and the 2026-10-01 deploy record, "Effective iter env").
I did not re-read the env file, because it holds secrets: `ITER_CONCURRENCY_ENABLED=1`,
`ITER_MAX_BACKGROUND_BRANCHES=3`, `ITER_MAX_OPEN_REQUESTS=3`. The 2026-10-01 record
says `ITER_MAX_CONCURRENT_LLM_CALLS` and `ITER_BG_LLM_ATTEMPTS` are unset, so their
defaults apply. **[UNVERIFIED]** that this is still true.

**Observed usage** (`~/.openclaw/protocosmo2-supervisor.log`, which has no timestamps;
these are grep counts):

- Since the current process started (last `SHUTDOWN_COMPLETE`, log line 1773800):
  about 930 `BEFORE LLM` lines and exactly **1 promotion**, `LLM_PROMOTED:
  branch_id=bg-aa3f5807 … active=1/3`. That branch ended with `BRANCH_BACKSTOP …
  steps=18 reason=its last 8 steps repeated …`, then `BRANCH_BACKSTOP_AFTER_ANSWER`,
  then `BRANCH_COMPLETE steps=10`. guard.steps = 18 = 8 foreground steps (forked) +
  10 branch steps. There were no real `BACKGROUND_ABANDONED`, `BACKGROUND_ERROR` or
  `CHANNEL ERROR` events. Lines that look like them after 1773800 are the agent's own
  shell output quoting older log lines.
- Over the whole log, multi-branch format (`active=k/N`, deduplicated by branch id):
  140 promotions. 135 had `active=1/3`, 4 had `2/3`, 1 had `3/3`. So three
  simultaneous branches have happened, but only rarely.
- Only a **model call** that takes longer than 30 s is promoted. Tool calls never
  trigger promotion. In a turn that is not promoted, the model call runs in a helper
  thread that the main thread joins, and **all tool calls run on the main thread**.
  Note that tool calls do run on branch threads after a promotion: the mini-loop
  executes its own tools (`iter.py:1330-1361`). Most live work is sequential on the
  main thread. Today, promotion is rare because most gateway calls finish in under 30 s.

---

## 2. Architecture

### 2.1 Threads

| Thread | Count | Runs | May write |
|---|---|---|---|
| Main | 1 | receive, foreground turn, tools of non-promoted turns, drain, deadline check, request closing, checkpoint file writes, shutdown | `experience` / `experience.json` (only writer), `_foreground_request_ids`, `_active_branches` (under lock) |
| Call thread (`_bg_llm_thread_target`) | 1 per model call while concurrency is on | one model call; if promoted, then becomes the branch and runs `_bg_branch_mini_loop` | `result_container`, `_merge_queue`, `_active_branches` (via `_release_branch`, under lock) |
| Tool subprocesses | 1 per tool/channel call | `iter.py --invoke …` (`invoke_dynamic`, 477-516); env carries `ITER_REQUEST_IDS` when bound | channel files (atomic), tool state |

All call threads are `daemon=True` (`iter.py:1455-1459`), so they cannot block exit.

### 2.2 Lifecycle of a promoted call

```
main loop (iter.py:1808)
 ├─ graceful_shutdown() if _shutdown_event           1810
 ├─ check_background_deadline()                       1828   abandon idle branches
 ├─ drain_merge_queue()            [drain a]          1831
 ├─ pop_branch_error() → raise into error handling    1836-1838
 ├─ receive() (claims ≤1 request for the foreground)  1840 → 561
 ├─ idle gate: branch active & foreground owns no request → sleep 1s, continue   1844-1849
 ├─ _note_inbound(event) → every running branch's inbox                          1864-1865
 └─ inner loop: build messages
      ├─ drain_merge_queue()       [drain b]          1909
      ├─ sibling_view(None) appended                  1910-1912
      └─ threaded_llm_call(...)                       1920 → 1440
            thread.start(); join(30 s)
            ├─ done in time → (response, None) → normal turn; tools on main thread
            ├─ branch set full → LLM_PROMOTION_SKIPPED, join(BG_INFLIGHT_LIMIT) in foreground   1463-1469
            └─ still running → PROMOTE                                          1471-1508
                 request_ids = copy(_foreground_request_ids); fork StepGuard;
                 branch_id=bg-<8hex>; deepcopy(messages); new OpenAI client;
                 _active_branches[id]=BranchState; _foreground_request_ids.clear()
                 return (None, branch) → main: _promoted → new_burst=True → continue  1948-1952

call thread, after promotion (1242-1259 → 1284):
  _tool_context.request_ids = branch's ids              1297-1298
  loop (≤ BRANCH_STEP_BUDGET = HARD_STEP_CEILING = 300):
    stop if abandoned                                   1310-1313
    put assistant msg on _merge_queue (tagged)          1319
    no tool calls → text-only fallback if never sent → break   1322-1328
    run tools (bound sends) → put tool msgs on queue    1330-1361
    StepGuard.record_step → stop / nudge / warn / fallback      1365-1377
    inbox (follow-ups) + nudge + fresh sibling_view → next call 1381-1389
    _bg_llm_create(...) (semaphore, in-flight mark, retry)      1393-1397
    on error → error marker + record_branch_error + _release_branch("error")  1399-1416
  backstop without answer → queue checkpoint payload    1421-1433
  _release_branch("complete")                           1436

main thread, later:
  drain_merge_queue(): append tagged entries (single writer), R16 supersede marks,
     write+send checkpoint payloads (bound to the branch's request),
     _close_branch_requests(done) → close_active(request_id) with "unfinished" note   1533-1623
```

### 2.3 Request binding

- `_foreground_request_ids` (`iter.py:96`) maps channel stem to request_id for the
  foreground turn.
- In binding mode (`ITER_REQUEST_BINDING`, `iter.py:92`), `receive()` uses
  `receive_request()` and claims **at most one** foreground request per channel.
  While that request is still open (`_foreground_busy`, 536-545, checked with
  `active_request_ids`), new messages wait in the inbox.
- The main thread binds `_tool_context.request_ids` to **the same dict object**
  (`iter.py:1802`). Tool subprocesses get `ITER_REQUEST_IDS` (JSON) from `_tool_env()`
  (468-475), and `tools/send.py:26-39,61-66` routes `send` to exactly that request.
  With binding on, a turn that owns no request is refused with "No open … request is
  bound to this turn".
- **F4:** binding is on whenever `ITER_CONCURRENCY_ENABLED` is on (was: only with more than one
  branch). With 1 branch and no binding the channel held one request, so a chat message could
  not be claimed while a promoted branch owned the open request.
- On promotion, the branch takes a copy of the ids and the foreground dict is cleared
  (`iter.py:1474,1503-1504`), so from then on only the branch can answer that request.
- Main-thread sends on behalf of a branch (checkpoint messages) temporarily rebind with
  `_call_bound` (1209-1218).
- Request closing:
  - The foreground closes only its own request at turn end
    (`close_unfinished_requests`, 618-653, after a nop or a backstop).
  - A finished, failed or abandoned branch hands its ids to the main thread through a
    `_branch_done` queue item. `_close_branch_requests` (1220-1230) then closes them
    with `close_active(request_id=…)`. That is a no-op if the branch already sent a
    final answer, and it is skipped if the foreground owns that id again.
- Startup: `_resume_bound` (547-559) resumes the oldest open request and closes the
  rest (`RESUME_EXTRA_REQUEST_CLOSED`). **Consequence:** a restart closes requests that
  branches were working on, with the "Iter stopped working…" note.

### 2.4 Model-call semaphore

`_llm_call_semaphore = BoundedSemaphore(ITER_MAX_CONCURRENT_LLM_CALLS)` (`iter.py:114`).
Every foreground and branch call goes through `_capped_llm_create` (116-124), which
waits at most `LLM_TIMEOUT` (600 s) for a slot.
- Branch calls also go through `_bg_llm_create` (143-167). It sets
  `llm_inflight_since`, so the idle deadline does not drop a branch whose call is still
  running, and it retries retryable errors up to `BG_LLM_ATTEMPTS`, but only once the
  call has been promoted.
- The default number of slots is branches + 1, which adds no limit beyond the branch
  cap.

### 2.5 Shutdown (R17)

1. A SIGTERM or SIGINT handler sets `_shutdown_event` (1681-1706). Handlers are
   installed only when concurrency is on (1806-1807).
2. At the top of the next loop, `graceful_shutdown()` (1708-1729) waits up to
   `ITER_SHUTDOWN_GRACE` (5 s) until every branch's `result_container` has an `ok` key.
3. It then drains the queue, saves, and calls `sys.exit(0)`.

**[CODE-READING]** After a branch's first response, `ok` is already set. So the grace
period only covers branches whose first call is still running. A branch that is
mid-way through its mini-loop is killed (daemon thread), and its request is closed on
the next start by `_resume_bound`.

---

## 3. Component map (live `iter.py`)

| Lines | Name | Role |
|---|---|---|
| 42-57 | `_env_int`, `PROGRESS_*`, `HARD_STEP_CEILING`, `REPEAT_STEPS`, `STALL_STEPS` | per-request step policy shared by foreground and branches |
| 66-70 | `ITER_CONCURRENCY_ENABLED`, `ITER_PROMOTE_SECONDS`, `BACKGROUND_DEADLINE` | master flag, promotion deadline T, idle deadline `max(2T,300)` |
| 74 | `BRANCH_STEP_BUDGET = HARD_STEP_CEILING` | branch mini-loop bound (was 25) |
| 77-89 | `_max_background_branches` | branch cap, clamp 1..8, invalid → 1 |
| 92-96 | `ITER_REQUEST_BINDING`, `ITER_MAX_OPEN_REQUESTS` setdefault, `_foreground_request_ids` | binding mode |
| 101-124 | `_max_concurrent_llm_calls`, `_llm_call_semaphore`, `_capped_llm_create` | model-call cap |
| 129-167 | `_bg_llm_attempts`, `BG_INFLIGHT_LIMIT`, `_bg_llm_create` | in-flight mark + branch retry |
| 170-173 | `RETRY_BACKOFF_*`, `BRANCH_IDLE_POLL_SECONDS` | backoff + idle poll |
| 431-432 | `_STRUCTURED_RESULT_FUNCTIONS` | channel hooks whose results are returned as JSON, not `str()` |
| 434-464 | `dynamic_worker` | subprocess side of `invoke_dynamic` |
| 466-475 | `_tool_context`, `_tool_env` | per-thread binding → `ITER_REQUEST_IDS` |
| 477-516 | `invoke_dynamic` | tool/channel subprocess (DYNAMIC_TIMEOUT 30 s) |
| 529-534 | `_channel_has` | feature probe; **live version greps source** (see §6 B5) |
| 536-559 | `_foreground_busy`, `_resume_bound` | one foreground request; startup recovery |
| 561-586 | `receive` | `receive_request` in binding mode, else `receive` |
| 588-614 | `resume_claimed` | startup resume |
| 616-653 | `ITER_REQUEST_TIMEOUT`, `close_unfinished_requests` | turn-end `expire_stale` + close own request |
| 805-913 | terminal/retryable classification, backoff, exhausted notice | shared with branch errors |
| 915-948 | `unwrap_pseudo_send`, `send_text_only_fallback` | deliver text-only endings |
| 951-1046 | `step_signature`, `StepGuard` (`fork` 990) | per-request guard; forked into branches |
| 1048-1066 | `send_progress_note`, `_foreground_step_guard` | non-closing mechanical note (bound) |
| 1068-1151 | `_sibling_lock`, `_branch_activity`, `_recent_sends`, `_note_send`, `_note_inbound`, `_register_branch_activity`, `_mark_branch_activity_done`, `take_branch_inbox`, `sibling_view` | thread visibility |
| 1153-1176 | `background_branch_active/count`, `record_branch_error`, `pop_branch_error` | branch state queries; error hand-off |
| 1180-1194 | `_branch_lock`, `_active_branches`, `BranchState` | branch registry |
| 1196-1207 | `_release_branch` | free slot (pop by id, once) + queue `_branch_done` |
| 1209-1218 | `_call_bound` | temporary rebinding on the main thread |
| 1220-1230 | `_close_branch_requests` | close what a branch left open |
| 1232-1281 | `_bg_llm_thread_target` | call thread; error marker (R14) |
| 1284-1437 | `_bg_branch_mini_loop` | branch loop (R20 + progress-not-pause) |
| 1440-1514 | `threaded_llm_call` | join(T), cap check, promotion |
| 1517-1623 | `_merge_queue`, `_build_tool_call_map`, `drain_merge_queue` | single-writer merge, R16 supersede, checkpoint + done payloads |
| 1626-1675 | `check_background_deadline` | R13 idle deadline (progress-based, in-flight aware) |
| 1678-1729 | `SHUTDOWN_GRACE`, signal handler, `graceful_shutdown` | R17 |
| 1801-1807 | main-loop setup | StepGuard, foreground binding, `resume_claimed`, signal handlers |
| 1808-2071 | main loop | see §2.2; foreground `_note_send` 1987-1988; nop → `close_unfinished_requests` 2013; backstop → 2034 |

Outside `iter.py`:

| File | Role |
|---|---|
| `REPO/tools/send.py:12-73` | `final` flag; `ITER_REQUEST_IDS` routing; refuses an unbound turn; "SUCCESS (progress; request still open)" for final=false |
| `REPO/channels/protocosmo2.py` | shim. Imports `receive, resume, send` directly. Imports `close_active, expire_stale` and `active_request_ids, receive_request, resume_all` inside `try/except ImportError` |
| `REPO/tools/wmtm_derive.py:26-34`, `REPO/transformations/wmtm_context.py:35-47` | `fcntl.flock` on shared WMTM pending-derives state (concurrent tool subprocesses) |
| `REPO/transformations/wmtm_context.py:156-157,502` | skips the injected `[OTHER THREADS …` / `[NEW USER MESSAGE(S) …` blocks when it looks for the real user message (F35) |

---

## 4. Configuration

| Env var | Default | Clamp / parse | Live value | Where |
|---|---|---|---|---|
| `ITER_CONCURRENCY_ENABLED` | `0` | `== "1"` | `1` | iter.py:67 |
| `ITER_PROMOTE_SECONDS` (T) | 30 | `int()`, no clamp (a bad value crashes startup) | default **[UNVERIFIED]** | :68 |
| `BACKGROUND_DEADLINE` | `max(2T, 300)` | derived; not an env var | 300 | :70 |
| `ITER_MAX_BACKGROUND_BRANCHES` | 3 | 1..8; invalid → 1 | 3 | :79-89 |
| `ITER_REQUEST_BINDING` | derived: concurrency on and branches > 1 | — | on | :92 |
| `ITER_MAX_OPEN_REQUESTS` | iter sets `setdefault(branches+1)` (= 4); the channel's own default is 1, clamp 1..9 | channel: `int`, else `ChannelContractError` | **3** (env file; so with 3 branches holding requests the foreground waits) | iter.py:95; PORT/src/iter_outer_channel_protocosmo2.py:165-179 |
| `ITER_MAX_CONCURRENT_LLM_CALLS` | branches + 1 | 1..default; invalid → default | unset → 4 | :101-113 |
| `ITER_BG_LLM_ATTEMPTS` | 2 | 1..5; invalid → 2 | unset → 2 | :129-139 |
| `BG_INFLIGHT_LIMIT` | `2*LLM_TIMEOUT+60` = 1260 s | constant | — | :140 |
| `LLM_TIMEOUT` | 600 s | constant | — | :24 |
| `ITER_REQUEST_TIMEOUT` | 3600 s | `float`; ≤0 disables | default **[UNVERIFIED]** | :616 |
| `ITER_SHUTDOWN_GRACE` | 5 s | `int` | default | :1678 |
| `ITER_SIBLING_SEND_WINDOW` | 1800 s | `int` | default | :1071 |
| `ITER_HARD_STEP_CEILING` / `ITER_REPEAT_STEPS` / `ITER_STALL_STEPS` | 300 / 8 / 12 | min 3 / 2 / 2 | defaults (per 2026-10-04 record) | :52-54 |
| `ITER_PROGRESS_EVERY` / `_FALLBACK_STEPS` / `_MAX_SILENT_SECONDS` | 30 / 2 / 420 | min 3 / 1 / 60 | defaults | :49-51 |
| `ITER_RESPONDER_MAX_SECONDS` (receiver) | 10800 | float ≥0 | default | PORT/src/iter_responder.py:29-33 |
| `--iter-timeout` (runner) | 1500 s (was 900 until 2026-10-05) | — | default | RUNNER:1142-1146 |
| `ITER_LATE_REPLY_MAX_AGE_SECONDS` (runner) | 21600 | float | default | RUNNER:1080 |

Not concurrency flags, but they interact with it: `DYNAMIC_TIMEOUT=30`
(iter.py:183) bounds every tool and channel subprocess, and
`BRANCH_IDLE_POLL_SECONDS=1` (:173).

---

## 5. Channel / transport contract required by the port

### 5.1 Channel module (`PORT/src/iter_outer_channel_protocosmo2.py`, at PORT commit d574cc9 / 19d0dc3)

| Function | Lines | Semantics the port relies on |
|---|---|---|
| `receive_request()` | 198-215 | If fewer than `max_open_requests()` are in `processing/`, move the oldest inbox file there, touch its mtime, return `{request_id, prompt}`; else `None`. More than the cap → error. **F9:** "oldest" is by (mtime, name); d574cc9 took the lowest sha256 filename. |
| `receive()` | 218-225 | Legacy wrapper (prompt string). |
| `resume()` | 228-239 | Legacy: exactly one open request. |
| `resume_all()` | 242-246 | All open requests, oldest claim first (`[{request_id, prompt}]`). |
| `active_request_ids()` | 322-324 | Ids in `processing/`, oldest first. |
| `send(content, final=True, request_id=None)` | 282-319 | `final=True`: write `outbox/<id>.json`, move to `completed/` (closes). `final=False`: `progress/<id>.<seq>.json` (max 50/request), request stays open. A closed or unknown id is refused, so **a second final answer is impossible**. `request_id=None` with several open requests is refused. **F6:** d574cc9 checked `outbox.exists()` then overwrote, so two racing finals could both pass and the loser replaced the winner's answer; the outbox entry must be created exclusively (`os.link` of a temp file) and the loser refused. |
| `close_active(note, request_id=None)` | 337-351 | Close that request with the "Iter stopped working…" note; returns `False` if it is already closed, **or (F6) if another writer answered or closed it concurrently** (never raises for that race). |
| `expire_stale(max_age_seconds, …, keep=())` | 366-386 | Close requests idle (claim time or newest progress note) longer than `max_age_seconds`. **F7:** never closes an id in `keep` (requests a live thread owns). |
| `active_request_ids()` / `resume_all()` listing | 187-188 | **F6:** a request closing between `glob()` and `stat()` is skipped, not an error (an error made `_foreground_busy` drop the foreground's binding). |
| `mark_late_pending(request_id)` | 70-79 | Receiver side: marks a request whose synchronous wait timed out. |

**Results must stay structured.** `dynamic_worker` would otherwise call `str()` on
them. Names in `_STRUCTURED_RESULT_FUNCTIONS` (`iter.py:431-432,455-457`) keep their
JSON-native return value. Without this, `receive_request()` returns a dict repr, Iter
raises `TypeError: string indices must be integers`, and the request is stranded in
`processing/` (§6 B6).

**The shim must export the hooks.** `REPO/channels/protocosmo2.py` re-exports them.
Iter detects them with `_channel_has()`. fd065a3 does a **text grep** of the
shim, so a shim that *mentions* a hook in a guarded import is treated as having it,
even when the backing module lacks it (§6 B5). **F1** replaces the grep with a real
probe: the dynamic worker imports the channel and answers `__has_attrs__` with
`callable(getattr(module, name))` for each hook; results are cached per file mtime/size.

### 5.2 `tools/send.py` (`REPO/tools/send.py`)

`run(channel, content, final=True)`:
1. Parse `final` (bool or string).
2. If `ITER_REQUEST_IDS` is set and the channel's `send` accepts `request_id`, pass
   the bound id. If no id is bound, refuse.
3. `final=False` on a channel that has no `final` parameter is refused.

Return values: `"SUCCESS"` or `"SUCCESS (progress; request still open)"`.
`StepGuard.record_step` (`iter.py:1001-1004`) and the branch loop (1348-1351) use
those two prefixes to tell an answer from a progress note.

### 5.3 Receiver side (`PORT/src/iter_responder.py`, `iter_relay.py`; `RUNNER`)

- **Per-thread `bind()`** (PORT 88958e5; `iter_responder.py:64-72,88-92`). The
  receiver's deferred/promotable worker threads may handle several messages for the
  same chat at once. The ids passed to `bind` must be thread-local, or replies get
  attached to the wrong message.
- **Progress delivery and wait extension** (PORT 19d0dc3; `iter_responder.py:26-47,74-86,105-118`).
  Each new progress note extends the wait by `timeout_seconds`, up to
  `ITER_RESPONDER_MAX_SECONDS`. On timeout, `mark_late_pending` is called and
  `TimeoutError` raised. The runner supplies `deliver_progress`
  (`make_iter_progress_deliverer`, RUNNER:1030-1072), which posts each note as a reply
  to the bound message.
- **Late answers.** `iter_relay.collect_pending(deliver, max_age_seconds, final_prefix)`
  (`iter_relay.py:174-208`) runs every receiver cycle (RUNNER:1245-1259). It delivers
  answers that arrived after the timeout, prefixed with "(Late answer…)", skips markers
  older than 6 h, and keeps undelivered progress ahead of the final reply.
- **Receiver timeout.** `--iter-timeout` default is 1500 s (RUNNER:1142-1146). This is
  above Iter's worst single-call wait, `BG_INFLIGHT_LIMIT` = 1260 s. It was 900 s,
  which was less than that (§6 B12).

---

## 6. Change history ("do not reintroduce these bugs")

Order is chronological. "Backup" means a `REPO/iter.py.pre-<tag>-<stamp>` file, i.e.
the version *before* that change. A feature matrix (grep across the backups) confirms
the order below.

| # | When (PDT) | Change / bug fix | Source | Invariant it protects |
|---|---|---|---|---|
| C1 | 2026-09-04 | M2.1 threaded call + promotion at T; deep copy (R11); separate client with `x-branch-id` (R12) | 5fee852 | A slow call never blocks the channel longer than T; branches never share mutable message lists or clients |
| C2 | 09-04 | M2.2 merge queue, double drain (R15), branch tags (R16) | dd5ddae | **Only the main thread writes `experience`** |
| C3 | 09-04 | M3.1 `BACKGROUND_DEADLINE` (R13), M3.2 error markers (R14), M3.3 shutdown (R17), M3.4 supersede marks (R16), M3.5 branch step budget 25 + checkpoint via the main thread (R20) | 6a876bd, df213c6, 83c59de, 94fbe29, 23a5d6f | Bounded branch lifetime; errors become visible; branches never write files themselves |
| C4 | 09-06 | Flag enabled live after approval | PLAN.md 4.2 | — |
| C5 | 09-22 | `tool_choice` "required" → "auto" (fg, threaded, branch) | backup `pre-tool-choice-auto-20260922T2258Z` | (reverted in C14) |
| C6 | 09-22/23 | `AUTONOMOUS_CHECKIN_INTERVAL=900` throttle for idle post-task turns | backup `pre-autonomous-throttle-20260922T2320Z` (`pre-else-branch-throttle-20260923T1210Z` is byte-identical to `pre-branch-gate-backoff-…`) | Idle Iter makes at most one autonomous call per 900 s |
| **B1** | 09-23 | **Promotion flood.** Gateway calls took more than 30 s, so almost every call was promoted. The main loop immediately started another "continue" call, and each promotion **overwrote `_active_branch`**, so the 300 s deadline never fired. Result: 3,211 calls and 843 promotions on 09-22, and 429s retried every 1 s. Fix: (a) while a branch is active, the main loop only polls (a new user message may still get a foreground call); (b) promotion never replaces an active branch (`LLM_PROMOTION_SKIPPED`, wait in the foreground); (c) an abandoned branch stops its mini-loop and drops late results; (d) branch errors re-enter the main-loop error handling (`record/pop_branch_error`); (e) exponential backoff 5 s→300 s with jitter and Retry-After; idle after quota exhaustion or 3 capped waits; terminal 4xx idles | backup `pre-branch-gate-backoff-20260923T1823Z`; `EXP/20260923T1830Z-protocosmo2-branch-gate-backoff-router-fix/RUN.md`; `test_branch_gate_backoff.py` | **No unthrottled continuation while a branch owns the task. Never evict or overwrite a running branch. Abandon means stop. Provider errors from branches are backed off like foreground errors.** |
| C7 | 09-25/26 | `MAX_TOKENS` 2524 → 32000 | backups `pre-max-tokens-*` | — |
| **B2** | 09-26 | **Idle-loop churn**, 100-170 calls/h: `nop` re-armed `new_burst`, which bypassed the 900 s throttle, and text-only replies were re-prompted without limit. Fix: no re-arm during autonomous work; `TEXT_ONLY_RETRY_CAP=2`; the throttled idle path sleeps 1 s instead of spinning | backup `pre-idle-loop-fix-20260927T0003Z`; `EXP/20260927T0003Z-protocosmo2-idle-loop-fix/RUN.md`; `test_text_only_retry_cap.py` (added 10-05) | Idle stays cheap; a text-only reply cannot loop |
| C8 | 09-28 | Multi-send in iter: `send(final=)`, turn-end `close_unfinished_requests`, `expire_stale` (`ITER_REQUEST_TIMEOUT`) | fda9ac6, 61225e5 (branch `agent/protocosmo2-multi-send`); PORT 36367ee | A request is never left open forever; a progress note never closes a request |
| **B3** | 09-30 | **Idle deadline measured from creation**, so long but productive branches were killed at 300 s. Fix: `last_progress_at` is updated on every model response and tool result; the deadline is measured from it | 5fcd01a; backup `pre-multisend-idle-deadline-20260930T1915Z`; `test_background_idle_deadline.py` | Deadline = **idle** time, not age |
| C9 | 09-30 | Multi-branch: `_active_branches` dict, cap `ITER_MAX_BACKGROUND_BRANCHES`, request binding, `_release_branch` pop-by-id, `_close_branch_requests` | f1cb71d (spec R18 "one branch at a time" is superseded) | Each branch answers only its own request; a slot is freed exactly once |
| C10 | 09-30 | `flock` on WMTM pending-derives append + atomic read/clear | 18980d3, 7423976, 3156cde; `test_wmtm_derive_lock.py`, `test_wmtm_drain_lock.py` | Shared tool state is safe under concurrent tool subprocesses |
| C11 | 09-30 | `ITER_MAX_CONCURRENT_LLM_CALLS` semaphore | bc0ac63; `test_llm_concurrency_cap.py` | Provider protection; slot released on error |
| **B4** | 09-30 | **Hung-call drop** (`bg-89ff37ae`): the idle deadline dropped a branch whose single call was still in flight. Fix: `llm_inflight_since`; the deadline skips in-flight calls until `BG_INFLIGHT_LIMIT`; promoted branches retry timeouts and disconnects (`ITER_BG_LLM_ATTEMPTS`), but not quota errors and not after abandonment | 3c86713; `test_bg_llm_hang.py` | Never abandon a call that is still inside its own timeout; bounded retries |
| C12 | 09-30 19:01 | Model tag on every send (session-history lookup); deployed together with multi-branch | c234e28; `EXP/20261001T0200Z-protocosmo2-model-tag/RESULT.md` | — |
| **B5** | 09-30 19:01 | **Receive outage after deploy.** The 3c86713 shim imported `receive_request` etc. in a guarded `try/except ImportError`, but the deployed transport src (f2a588a) lacked them. `_channel_has` greps the shim text, saw the names, and called the missing hooks, so receive failed. Hot fix: restore the pre-multibranch shim (requests channel-serialized). Proper fix 6ea3dd6 (`__has_attrs__` probe of real callables, mtime-cached) is on `agent/protocosmo2-multi-branch` but was **never deployed**. The live `_channel_has` still greps (`iter.py:529-534`) and `close_unfinished_requests` still greps (`:626-640`) | `TASKS.md` (09-30 19:05 entry); backups `channels/protocosmo2.py.multibranch-broken-20260930T1905`, `.pre-multisend-20260930T1915Z`; 6ea3dd6 | **Deploy the transport hooks before (or with) a shim that names them; better, port 6ea3dd6's real-callable probe** |
| **B6** | 10-01 | **`str()` of structured hook results.** `receive_request()` came back as a dict repr, giving `TypeError` and a request stranded in `processing/`; `close_active`/`expire_stale` returned the string `"False"`, which is truthy. Found in staging; fixed before deploy with `_STRUCTURED_RESULT_FUNCTIONS` | backup `pre-multirequest-20261001T173614Z`; `EXP/20261001T1735Z-protocosmo2-multirequest-channel/README.md`; `test_invoke_structured_channel_results.py` | Channel hooks return JSON-native values through `--invoke`. Unit tests that stub `invoke_dynamic` miss this, so test through the real subprocess path |
| C13 | 10-01 10:38 | Multi-request channel live: PORT 511058f (cap, send/close by id), 88958e5 (per-thread bind); `ITER_MAX_OPEN_REQUESTS=3` | multirequest README | Up to N open requests; each answer goes to its own message |
| **B7** | 10-01 | **Branch text-only ending dropped.** The branch wrote its final `send` as plain text (`name=send arguments={…}`); the mini-loop treated "no tool calls" as done, nothing was sent, and the safety net closed the request with "Iter stopped working…". Fix: `unwrap_pseudo_send`; the branch tracks `branch_sent` and posts the text via `send_text_only_fallback` (bound) | backup `pre-branch-text-fallback-20261001T1941Z`; `EXP/20261001T1941Z-protocosmo2-branch-text-only-fallback/RUN.md`; `test_text_only_fallback.py` | **Every text-only ending of an unanswered request is delivered, in branches too** |
| C14 | 10-01 21:27 | Thread visibility: sibling view (other branches' tasks and recent sends), per-branch inbox for follow-up messages; `wmtm_context.py` skips these injections (F35) | backups `pre-thread-visibility-20261002T0427Z` (**this backup already contains the change**; `.reconstructed` holds the reconstructed pre-change file), `transformations/wmtm_context.py.pre-sibling-skip-20261001`; `test_thread_visibility.py`; snapshot commit 1ce3652. No dedicated experiment record found | Branches do not answer the same thing twice; a follow-up reaches the branch it belongs to; injected visibility text is never mistaken for user input |
| **B8** | 10-04 | **Branch 25-step stop.** Since most long tasks run as branches, 25 steps was the real limit, and the stop closed the request with a checkpoint. Replaced by progress-not-pause: a forked `StepGuard` per branch, interim notes (`send final=false`), mechanical fallback note, stops only at 300 / 8 identical steps / 12 stale steps; `BRANCH_STEP_BUDGET = HARD_STEP_CEILING`. Channel `expire_stale` measures from the newest progress note; the responder extends its wait on progress | backup `pre-progress-not-pause-20261005T0256Z`; 7dcbae7; PORT 19d0dc3; `EXP/20261005T0256Z-protocosmo2-progress-not-pause/RUN.md`; `test_progress_not_pause.py` (33) | Long work continues with visible progress; only runaway backstops stop it; a branch inherits the request's step count |
| C15 | 10-05 11:04 | `tool_choice` back to "required" (fg, threaded, branch) | backup `pre-tool-choice-required-20261005T1804Z` (diff = exactly 3 lines). No record found | — |
| **B9** | 10-05 | **Receiver timeout shorter than the worst call.** 900 s < 1260 s, so the person got a failure text and the late answer was never delivered (nothing called `collect_pending`). Fix: `--iter-timeout` 1500; `collect_pending` every cycle with a 6 h max age and a "(Late answer…)" prefix | `EXP/20261005T2023Z-iter-long-request-audit/RUN.md` (follow-up 14:02); PORT 19d0dc3; runner backup `.pre-late-reply-20261005T2057Z`; `PORT/tests/test_runner_iter_timeout.py` | **Receiver timeout > BG_INFLIGHT_LIMIT. Late answers are always delivered (bounded by age)** |
| — | 10-05 | Observed: branch `bg-bc0476a2` and the foreground both tried to answer msg 12454. The channel refused the second final ("request is not active") | audit RUN.md last bullet | Channel-level single-final protection is the last line of defence; keep it |

---

## 7. Invariants / safety rules

1. **One receiver per bot.** Exactly one Iter, one receiver and one supervisor per
   identity. A duplicate tree (a recurring problem on Pop!_OS) gives Telegram 409s and
   two channel claimers. Check topology after every restart.
2. **Single writer.** Only the main thread appends to or saves `experience`, writes
   checkpoint files, closes requests and touches `_foreground_request_ids`. Branches
   communicate only through `_merge_queue`, `result_container`, `_branch_error` and the
   locked sibling state (R6/R15/R20).
3. **Isolation.** A branch gets `copy.deepcopy(messages)` and its own OpenAI client
   (R11/R12). `StepGuard` is forked with `deepcopy`.
4. **Caps.**
   - Branch set ≤ `ITER_MAX_BACKGROUND_BRANCHES` (≤8).
   - Model calls ≤ `ITER_MAX_CONCURRENT_LLM_CALLS`.
   - Open requests ≤ `ITER_MAX_OPEN_REQUESTS` (≤9).
   - At most one foreground request per channel.
   - When the set is full, wait in the foreground. Never evict a branch.
5. **Deadlines.**
   - Idle deadline 300 s from last progress.
   - In-flight calls are kept until `BG_INFLIGHT_LIMIT`.
   - Each request expires after 3600 s without claim or progress activity.
   - Abandon means the thread stops and its results are dropped.
6. **No unthrottled continuation.** While a branch owns the task and the foreground
   owns no request, the main loop only polls (1 s). Idle autonomous turns run at most
   every 900 s. Text-only replies are re-prompted at most twice. Provider errors back
   off exponentially.
7. **Binding.** Every send carries the thread's own request id. An unbound turn cannot
   answer. After promotion the foreground releases the ids. A request is closed by its
   owner, or by the main thread on the owner's behalf, and only once (the channel
   refuses a second final answer).
8. **Text-only fallback** in both the foreground and branch paths, including a send
   written out as plain text.
9. **Progress, not pause.** Interim notes do not close the request (`final=false`).
   Only the 300 / 8 / 12 backstops stop work, and each states its reason.
10. **Receiver timing.**
    - Responder timeout > worst single-call wait (`BG_INFLIGHT_LIMIT`).
    - Progress extends the wait.
    - Late answers are delivered.
11. **Structured channel results** through `--invoke`, and feature detection that
    reflects real callables.
12. **Shared files** touched by tools or transformations need locks (flock) or atomic
    writes, because tool subprocesses from different threads can overlap.
13. **Flag off = old behaviour.** With `ITER_CONCURRENCY_ENABLED=0`:
    - the direct `_tracked_llm_create` path is used (iter.py:1924-1925);
    - no drains or deadline checks run;
    - no signal handlers are installed;
    - binding is off.
14. **Owned requests and merged history survive failures (F7, F8).** Request expiry closes
    only orphans: the foreground's request and every active branch's are passed as
    `keep`. A hung branch is bounded by `BACKGROUND_DEADLINE`, which closes its request.
    A failed model call rolls back the foreground's partial turn but keeps branch
    entries that drain (b) merged after the checkpoint.

---

## 8. Tests

**How to run** (on a copy, not the live tree; tests write temp files and caches):
`cd <copy>/repos && python3 -m pytest -q test_multi_branch.py test_progress_not_pause.py test_bg_llm_hang.py test_background_idle_deadline.py test_branch_gate_backoff.py test_llm_concurrency_cap.py test_invoke_structured_channel_results.py test_text_only_retry_cap.py test_text_only_fallback.py test_close_unfinished.py test_thread_visibility.py test_send_final.py test_wmtm_derive_lock.py test_wmtm_drain_lock.py test_terminal_provider_circuit.py ../tests`.

Most iter tests load single functions from `iter.py` with `ast` and `exec` them in a
stub namespace. `iter.py` must not be imported, because import starts the main loop.
`progress_harness.py` runs the real main loop against a fake OpenAI client and the
real channel/shim/send in a temp tree. `HARNESS_ITER_PY` selects the iter.py under
test, for negative controls.

| File (count) | Covers |
|---|---|
| `test_multi_branch.py` (12) | cap clamp; promote below cap and bind the fg request; full set waits in fg without evicting; only idle branches abandoned; release once and hand back the request; bound send / unbound refused / legacy unchanged; one fg request at a time; turn end closes only the fg request; done-skip when fg owns it; 3 open requests each answered by its binding |
| `test_branch_gate_backoff.py` (10) | B1: poll instead of calling while a branch is active; never overwrite; abandon stops; branch errors reach the main loop; backoff and jitter; Retry-After; quota detection |
| `test_background_idle_deadline.py` (4) | B3 idle-deadline semantics |
| `test_bg_llm_hang.py` (11) | B4 in-flight marker, retry bounds, no retry when not promoted, non-retryable, or abandoned |
| `test_llm_concurrency_cap.py` (4) | semaphore cap, release on error, slot timeout |
| `test_invoke_structured_channel_results.py` (2) | B6 via the real `--invoke` path |
| `test_text_only_fallback.py` (9) | B7, including `test_branch_loop_delivers_unanswered_text_only_ending` and pseudo-send unwrap |
| `test_text_only_retry_cap.py` (5) | B2 retry cap (main-loop block extracted with ast) |
| `test_close_unfinished.py` (5) | `expire_stale` / `close_active` turn-end hook |
| `test_thread_visibility.py` (6) | sibling view, inbox delivered once, wiring in both loops |
| `test_progress_not_pause.py` (33) | B8 guard, branch progress/ceiling/fork, harness scenarios, responder extension, runner `deliver_progress` |
| `test_send_final.py` (5) | `send` final flag on old and new channels |
| `test_wmtm_derive_lock.py` (2), `test_wmtm_drain_lock.py` (1) | C10 flock |
| `test_terminal_provider_circuit.py` (5) | terminal-error breaker, incl. wrapped branch errors |
| `PORT/tests/test_iter_outer_channel_multiopen.py` (7), `…_multisend.py` (13), `test_iter_responder.py` (5), `test_iter_relay.py` (8), `test_runner_iter_timeout.py` (6) | channel cap/ids/final/progress/expiry; per-thread bind; late replies; 1500 s default and wiring |

**Recorded results (I did not re-run them; the run was not permitted in this session):**
- full `pytest . ../tests`: 904 passed, 1 skipped (2026-10-05, audit record), then 911
  passed, 1 skipped after the late-reply change.
- fd065a3 commit message: "all pass; progress_not_pause 33/33".
- PORT 19d0dc3 commit message: "tests/: 58 passed".

**Known gaps** (audit record):
- the end-to-end interaction between a long in-flight call and the receiver timeout;
- the `BACKGROUND_DEADLINE` formula (tests inject 300);
- real Telegram delivery.

Some tests reference `../wt-multibranch-channel` (`test_multi_branch.py:20`) and skip
or degrade if it is missing **[UNVERIFIED which]**.

---

## 9. Porting guide for VM2 (Protomega, Protomega2)

### 9.1 Snapshot used and its age

- `EXP/20261003T1600Z-iter-progress-not-pause/staged/{protomega,protomega2}/` is the
  code deployed **2026-10-03 09:16 PDT**. Both `iter.py` files are identical
  (750 lines, sha256 927211ed…). Baseline (652 lines) is in `baseline/`.
- Older: `EXP/20260929T1910Z-pdf-receiver-upgrade/{baseline,staged}/vm2-protomega*/`
  (554 → 638 lines, plus runner copies).
- **[UNVERIFIED] Drift since 10-03.** NOTES.md (2026-10-08, tag-rule entry) gives the
  prompt-load line as Protomega `iter.py` L599, which matches the snapshot, but
  **Protomega2 L749**. So Protomega2's `iter.py` has probably changed since the
  snapshot. Re-snapshot both from VM2 (read-only) before porting.

### 9.2 Differences that affect the port (VM2 snapshot vs laptop live)

| Area | VM2 (10-03 snapshot) | Laptop | Port impact |
|---|---|---|---|
| Threading | none: no `threading`/`queue` imports; direct `client.chat.completions.create(...)` (staged iter.py:610) | full machinery | port §3 whole |
| Model timeout | `LLM_TIMEOUT = ITER_PROVIDER_TIMEOUT_SECONDS` default 120 (:21); Protomega2 env 60 per `EXP/20260920T215500Z-protomega2-timeout-fallback-repair` | 600 | `BG_INFLIGHT_LIMIT` = 2·LLM_TIMEOUT+60 becomes 300 s or 180 s. Re-derive the receiver timeout from it. T=30 s promotes a large share of calls when LLM_TIMEOUT is 60 |
| Provider attempts | `ProviderAttemptLimit` after `ITER_MAX_PROVIDER_ATTEMPTS` (code default 8, :54; Protomega2 env = 2 per the 09-20 record; Protomega **[UNVERIFIED]**); counter reset after each success (:615); failure → `publish_terminal_failure` via channel `notify` | backoff/idle machinery (B1) + `record/pop_branch_error` | decide one policy. Branch errors must reach the VM2 attempt counter or terminal notice; otherwise a failing branch loops |
| Step policy | progress-not-pause already in place (`HARD_STEP_CEILING` 300, `MAX_FAST_STEPS` alias, :47-52); inline counters, no `StepGuard` class | `StepGuard` class with `fork()` | needs a per-thread guard object. The brief's "MAX_FAST_STEPS 30-step ceiling" is out of date since 10-03 |
| Idle | provider-silent: no autonomous post-task turns (:564-572) | 900 s throttled check-ins | keep VM2's. The B1 idle gate still matters for "CONTINUE THE CURRENT USER TASK" turns |
| `tool_choice` | "required" (:610) | "required" | same |
| `DYNAMIC_TIMEOUT` | 5 s (:64) | 30 s | channel and tool calls from several threads compete; consider raising it |
| `invoke_dynamic` | `(path, function, …)` (:194), no `env=` | `(path, /, function, …)` + `env=_tool_env()` | add positional-only `path` and `env` for binding |
| `dynamic_worker` | `str()` on everything except `transform` (:184-185) | `_STRUCTURED_RESULT_FUNCTIONS` | **port B6 first** |
| `receive()` | plain `receive`; CHANNEL ERROR only printed, never an event (:254-258) | returns `[CHANNEL ERROR…]` as an event | keep VM2's "a diagnostic never authorizes a provider call" |
| Startup | no `resume_claimed`, no signal handler | `resume_claimed` / `_resume_bound`, R17 | needs a recovery decision for branch-owned requests on restart |
| Channel contract | same filename, **different contract** (367 lines): single request (`receive` refuses while `processing/` non-empty); **first `send` closes**; later sends are follow-ups to `last_completed.json` (max 2, +1 for `notify`); `progress()` writes `followups/<id>.progress-NNN.json` (max 12, 3500 chars); `notify`, `deliver_reminder`, `reminder_source`; **no** `receive_request`/`resume_all`/`active_request_ids`/`close_active`/`expire_stale`/`request_id`/`final` | multi-request contract (§5.1) | the biggest item. Either port PORT 36367ee + 511058f onto the VM2 channel (keeping followups, progress, notify, reminders) or add request ids to the VM2 contract. Without a multi-request channel, run branches=1 (binding off) |
| `send.py` | no `final`, no binding (`baseline/protomega/send.py`) | §5.2 | port, keeping VM2's separate `progress` tool |
| `progress` tool | separate tool → channel `progress()` | `send final=false` | keep VM2's. The branch `branch_sent` / `StepGuard.answered` logic must treat `progress` SUCCESS as "noted, not answered" |
| Request-closing notices | `publish_terminal_failure`/`notify` with a delivery count check; `publish_no_response_notice` on nop without send (:412-424); duplicate-event suppression (:529-543) | `close_active` | these all assume exactly one open request. In binding mode they must target the thread's request id |
| Main-loop extras | `fire_due_alarms()` writes `experience` (:263-322); `current_event_message` pin (:519-522) | — | both are main-thread only; keep them out of branches (single writer) |
| Model tag | `response.served_model` (patched gateway dist on VM2) | session-history lookup | branch sends must use the tag of the branch's own response |
| Responder | `bound` is a **shared dict, not thread-local** (staged `iter_responder.py:61-67`); waits while progress is fresh | per-thread bind (88958e5) | **port 88958e5**: the VM2 receiver already runs deferred worker threads |
| Runner | `--iter-timeout` code default 900 (09-29 snapshot runner:655); 1800 in effect per `EXP/20261009T0024Z-protomega2-failure/RUN.md` (passed by launcher **[UNVERIFIED]**); `collect_pending` and `collect_followups` every cycle (:759-762) | 1500 + late-reply prefix | set timeout > new `BG_INFLIGHT_LIMIT` |
| Receiver liveness | transport-core `_reconcile_deferred_worker` treats Iter as alive only if `experience.json` changed ≤90 s ago (`DEFERRED_PROGRESS_MAX_STALE_SECONDS`) → false failure notices (10-09 record) | — | with branches, the main thread does not save `experience` until it drains. Branch progress must refresh this signal, or the check must change, or false failures get worse |
| Shared tool state | no WMTM; reminders (`memory/alarms/`), followups | WMTM flock | review every VM2 tool that does read-modify-write on files |

### 9.3 Recommended order

1. **Re-snapshot VM2 read-only.** Get both `iter.py`, `src/*`, the shims, `tools/`,
   the runner and the launcher env names (no values). Record sha256 values and diff
   them against the 10-03 snapshot.
2. **Transport first, with concurrency off.** Port to each VM2 channel:
   - request ids, a configurable cap (default 1), `receive_request`, `resume_all`,
     `active_request_ids`;
   - `send(…, request_id=)` and `close_active(request_id=)`;
   - `expire_stale` measured from progress;
   - keep `progress`/`followups`/`notify`/reminders bound to a request id.

   Also port per-thread `bind` (88958e5). Ship with cap=1 and check that behaviour is
   unchanged.
3. **Iter plumbing, still with the flag off:**
   - `_STRUCTURED_RESULT_FUNCTIONS` (B6);
   - the real-callable `_channel_has` probe from 6ea3dd6, not the grep (B5);
   - `_tool_context`/`_tool_env` and positional-only `invoke_dynamic`;
   - the binding-aware `send.py`;
   - a request-id target for `notify`/`publish_terminal_failure`.
4. **Concurrency core behind `ITER_CONCURRENCY_ENABLED` (default 0)**, using the
   *current* laptop code, not 5fee852:
   - `threaded_llm_call` with the full-set skip;
   - `_bg_llm_create` with the in-flight mark (B4) and the semaphore;
   - `_bg_branch_mini_loop` with a forked per-thread guard, text-only fallback (B7)
     and progress-not-pause (B8);
   - merge queue + double drain;
   - idle deadline (B3/B4);
   - branch errors wired into VM2's provider-attempt and terminal-notice policy (B1d);
   - the idle gate (B1a);
   - shutdown.
5. **Thread visibility** (C14), plus the matching skip in any transformation that
   looks for "the latest user message".
6. **Receiver:**
   - set `--iter-timeout` above the new `BG_INFLIGHT_LIMIT` (B9);
   - fix the 90 s `experience.json` liveness check so it also counts branch activity
     (or the iter process plus the request in `processing/`).
7. **Enable in stages:**
   - flag on with branches=1 (binding on, per F4, so `ITER_MAX_OPEN_REQUESTS`=2);
   - then branches=3 (`ITER_MAX_OPEN_REQUESTS`=4 by default).

   Keep `ITER_MAX_CONCURRENT_LLM_CALLS` low on the shared Anthropic account (VM2's
   history includes account exhaustion).

### 9.4 What to verify (hive canary convention)

- **Offline:**
  - port the laptop tests listed in §8, adapted to the VM2 contract;
  - add VM2 tests for `notify`/progress/reminders under binding;
  - run them as each agent uid inside the VM2 container (11003 / 11004), as in the
    10-03 record;
  - add a negative control: old code fails the new tests.
- **Test through the real `--invoke` subprocess path at least once** (B6 was invisible
  to stubbed tests).
- **Deploy:**
  - check live == baseline sha;
  - back up as `<file>.pre-concurrency-<stamp>`;
  - idle gate (inbox/processing empty, `experience.json` untouched ≥180 s);
  - **stop and start only via the owning VM2 controller**;
  - afterwards confirm exactly one iter.py and one receiver per bot id, no
    Conflict/Traceback/CHANNEL ERROR, and env without stray `ITER_` overrides.
- **Canary:**
  - first a fresh human message (fast path);
  - then one task > T while a second short message is sent (expect `LLM_PROMOTED
    active=1/N`, the short message answered first, the long one answered by its
    branch, `BRANCH_COMPLETE`, 0 `BACKGROUND_ABANDONED`, no duplicate answers);
  - then N tasks at once.
- **Rollback:** write the exact rollback commands into the record before deploying.
  The simplest rollback is `ITER_CONCURRENCY_ENABLED=0` plus a controller restart;
  the full rollback restores the backups.

### 9.5 Open questions and risks

1. **FIXED by F2 (2026-10-12). [CODE-READING] Promotion race.** In `threaded_llm_call`, `branch_id` is set
   (1479) *before* `branch_client`/`branch_messages` (1487-1488), with a `deepcopy` in
   between. If the call returns inside that window, `_bg_llm_thread_target` (1251-1259)
   skips the mini-loop without marking `finished`. The branch then sits until the
   300 s idle deadline abandons it, and its request is closed with the "stopped
   working" note. The answer is lost. The window is small and this has not been
   observed or tested. For the port, set client and messages before `branch_id`, or
   have the thread mark `finished` when it cannot start the mini-loop.
2. **FIXED on the laptop by F5. [CODE-READING] `tools/send_document.py` (`_source`) is not binding-aware.** It
   requires exactly one file in `processing/`, so it fails while 2 or more requests
   are open. Any VM2 tool that finds "the active request" by scanning `processing/`
   has the same problem; it must use `ITER_REQUEST_IDS`.
3. **[CODE-READING] `expire_stale` only runs at foreground turn ends** (nop or
   backstop). It does not run while the main loop sits in the branch-active idle gate.
4. **Restart closes branch-owned requests.** `_resume_bound` resumes only the oldest
   one. VM2 needs an explicit decision here, and so does the late-reply interplay.
5. **Choosing T on VM2.** Laptop T=30 came from a bimodal latency analysis
   (`iter-concurrency/m4-latency-analysis.md`: 61% of calls under 30 s). VM2's
   provider latency profile is unknown **[UNVERIFIED]**. Measure it first.
6. **Merge growth.** Branch entries are appended to the shared `experience` (window
   100/80). Busy branches can push the foreground's own request out of the window.
   VM2 pins `current_event_message`; decide how that works with several requests.
7. **Live acceptance** of glicerico's 3-concurrent-task test (2026-10-01 README) was
   never recorded as passed. The log shows one `active=3/3` promotion only.

---

## 10. Routing under concurrency (hive-route)

Hive-route (github.com/1-hive/hive-route; ADOPTING.md §10) routes an Iter agent **per
episode**: at each episode start the agent asks `POST /route` for a model, keeps that model
through every call and tool round trip of the episode, and reports `POST /episodes/end` with
the models that served it (plus a failure `class` when it didn't simply succeed). Protomega2
runs a hook for this (`HIVE_ROUTE` block in its `iter.py`, rev 13). All hive agents are meant
to use the router eventually, so this is the design every concurrent Iter should follow.

**The problem.** The current hook keeps **one global episode** (`_route = {"task", "episode",
"model", "served", ...}`). That assumes one request at a time. With concurrency, a promoted
branch and a new foreground request run at once and would overwrite each other's episode,
model and served-model set.

**Rules for a concurrency-aware hook.**
1. **Episode state is per request**, keyed by `request_id`, the same key request binding uses
   (`_foreground_request_ids` in the foreground, `BranchState.request_ids` in a branch).
   Keep it in a dict guarded by a lock; never in a module global.
2. **A new request starts a new episode** (`/route` once, at the turn that claims it).
3. **A promoted branch continues its request's episode**: same episode id, same routed model.
   The branch client must send the same routing header (`x-openclaw-model`) as the
   foreground did. Hive-route requires the model to stay fixed for the whole episode.
4. **The episode ends when its request closes**: final send, `close_active`, `expire_stale`,
   branch error, or restart release. Whoever closes the request calls `/episodes/end` once,
   with the models that actually served the episode's calls (foreground and branch).
5. **Failure class from the original exception** (F3): rate limit → `capacity` (with
   `limited_until` from Retry-After), outage → `outage`. After such a failure, end the episode
   and route a new one for the same task; don't retry the routed model.
6. **The fail-open breaker stays per agent** (it describes the route service, not a request),
   but its counter must be updated under a lock.
7. **Autonomous (unbound) turns** have no request: give them their own task/episode ids, and
   end the episode when the turn ends.

**Order.** On VM2, Protomega2's hook keeps its current form until the reader-shadow report
has been sent, so that the shadow's numbers come from one behaviour; then it is converted
together with the concurrency port. ProtoCosmo2 (laptop) routes through OpenClaw's
intent-model-router today and cannot reach the VM2 route service; moving it to hive-route
is a separate project that should reuse this design.

## 11. References

- Code:
  - `REPO/iter.py` (= fd065a3);
  - `REPO/tools/send.py`, `REPO/channels/protocosmo2.py`;
  - `PORT/src/iter_outer_channel_protocosmo2.py`, `iter_responder.py`, `iter_relay.py`, `iter_producer.py`;
  - `RUNNER`;
  - `REPO/tools/wmtm_derive.py`, `REPO/transformations/wmtm_context.py`;
  - `REPO/tools/send_document.py`.
- Iter commits:
  - M1-M3: 8aceac0, 2f79cab, c26da12, 5fee852, dd5ddae, 6a876bd, df213c6, 83c59de, 94fbe29, 23a5d6f;
  - multi-send: fda9ac6, 61225e5;
  - 5fcd01a; 347d849 (baseline);
  - multi-branch: f1cb71d, 18980d3, 7423976, 3156cde, bc0ac63, 3c86713, c234e28;
  - 6ea3dd6 (not deployed);
  - 1ce3652, 7dcbae7 (branch `agent/protocosmo2-progress-not-pause`);
  - fd065a3 (live sync).
- Branches: `agent/protocosmo2-multi-branch`, `-multi-send`, `-progress-not-pause`, `-checkout-sync-20261011`.
- Transport commits: 36367ee, 511058f, 88958e5, 19d0dc3, d574cc9.
- Backups: `REPO/iter.py.pre-*` (15 files), `REPO/channels/protocosmo2.py.pre-*` / `.multibranch-*`, `REPO/tools/send.py.pre-*`, PORT `src/*.pre-*`, `RUNNER.pre-*`.
- Design:
  - `protocosmo2/iter-concurrency/PLAN.md`, `m4-final-report.md`, `m4-latency-analysis.md`;
  - `projects/omegaclaw/docs/turn-budget-continuation-design-spec-v4.md` (R1-R20).
- Records (`EXP/`):
  - 20260923T1830Z-protocosmo2-branch-gate-backoff-router-fix;
  - 20260927T0003Z-protocosmo2-idle-loop-fix;
  - 20261001T0200Z-protocosmo2-model-tag;
  - 20261001T1735Z-protocosmo2-multirequest-channel;
  - 20261001T1941Z-protocosmo2-branch-text-only-fallback;
  - 20261003T1600Z-iter-progress-not-pause (VM2 snapshots);
  - 20261005T0256Z-protocosmo2-progress-not-pause;
  - 20261005T2023Z-iter-long-request-audit;
  - 20260929T1910Z-pdf-receiver-upgrade (older VM2 snapshots);
  - 20260920T215500Z-protomega2-timeout-fallback-repair;
  - 20261009T0024Z-protomega2-failure.
- Other notes: `projects/omegaclaw/TASKS.md`, `projects/omegaclaw/NOTES.md` (2026-10-08 entries).
- Runtime evidence: `~/.openclaw/protocosmo2-supervisor.log` (restart boundary at line 1773800).
