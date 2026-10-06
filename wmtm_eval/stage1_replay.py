#!/usr/bin/env python3
"""WMTM Stage 1 offline replay evaluation.

Replays a scripted multi-party thread (hive-appliance review cycle, with the
petta-chem batch as an interfering second topic and automated iter ticks in
between) into an ISOLATED WMTM store. Live memory/_wmtm_state.json and the
live journal are never written. At checkpoints it asks questions with known
answers and scores three conditions:

  none     - no memory injected (floor)
  keyword  - plain token-overlap search over the journal, top-15
  bridge   - WMTM RecallBridge ranking alone, top-15
  wmtm     - full WMTM path (feedback, recall+admission, cleanup, cycle);
             scored on the active set as injected into context (top 15 by STI)

Metrics: hit@1, hit@3, hit@15 (in context), MRR, stale rate (superseded
same-topic entry ranked above the current one / present in context),
utility inflation on automated ticks.

Usage: python3 wmtm_eval/stage1_replay.py [--no-distractors] [--out DIR]
"""
from __future__ import annotations
import argparse, calendar, json, sys, time, importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------- clock
class Clock:
    def __init__(self): self.now = time.time()
    def __call__(self): return self.now
CLOCK = Clock()
_real_time = time.time

def ts(s: str) -> float:
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))

# ---------------------------------------------------------------- scenario
# Journal notes: (key, iso time, topic, text)
NOTES = [
 ("h0", "2026-09-24T18:00:00Z", "hive", "Hive-appliance Astra review 6882 at 635fa3d found 11 findings (7 High, 4 Medium): A1 A2 H1 H2 U1 U2 U3 L1 S1 P1-symlink P2-store. Protomega2 to fix. Live repair and upgrade stay off."),
 ("p0", "2026-09-25T07:30:00Z", "petta", "petta-chem neutral calibration v2 batch status Sep 25: 19 of 32 shards done (g1001-g1014, g1017-g1021). queue_drain_watchdog relaunching g1015 g1016 g1022 every 10 minutes, max 3 slots."),
 ("h1", "2026-09-25T20:00:00Z", "hive", "Hive-appliance Astra re-review 6949 at bc8e5e0: 8 findings CLOSED, H2 U2 and P2-store PARTIAL. H2 untargeted receipt resolves incident; U2 rollback commands unused."),
 ("n0", "2026-09-26T02:00:00Z", "noise", "AUTOCYCLE heartbeat status check: nothing new, no user input, Nop."),
 ("h2", "2026-09-26T04:00:00Z", "hive", "Hive-appliance Opus 5.5 review 6979 at 1ab8e5f: 9 CLOSED, H2 and P2-store PARTIAL, U2 now CLOSED. New findings N1 (repair leaves incident open locally) and N2 (duplicate step index resolves locally). 623 passed, 1 offline packaging failure."),
 ("h3", "2026-09-26T05:00:00Z", "hive", "Hive-appliance Astra review 6986 at 1ab8e5f agrees with Opus 6979: 9 CLOSED, H2 and P2-store PARTIAL. N2 rated High, N1 Medium, both pre-existing at bc8e5e0."),
 ("w0", "2026-09-26T06:00:00Z", "wmtm", "WMTM status Sep 26: timeouts fixed, automated ticks under 1s, scores persist across restarts, stale items expire by journal date. Open: feedback loop inflating goal-status note utility."),
 ("h4", "2026-09-26T08:00:00Z", "hive", "Protomega2 pushed hive-appliance fixes as 44aaef8 for N1 N2 H2 and P2-store; tests/test_astra6986.py adds 16 tests, full suite 640 tests."),
 ("h5", "2026-09-26T12:00:00Z", "hive", "Hive-appliance Astra re-review 7003 at 44aaef8: 10 of 11 CLOSED, H2 PARTIAL; N1 N2 P2-store CLOSED. New N3 (High: buffered success, success, failure before PLAN lets hive resolve) and N4 (Medium: contradictory incident identity counted locally). Live repair stays off until N3 fixed."),
 ("p1", "2026-09-27T14:00:00Z", "petta", "petta-chem batch status Sep 27: watchdog stopped and replaced by tmux supervisor. Only 9 of 32 shards valid (g1001-g1009) because the runner was modified to skip failed rows. Fresh attempts from row 0 for g1015 g1016 g1022; g1023-g1032 queued; about 4 days per shard."),
 ("h6", "2026-09-27T15:00:00Z", "hive", "Protomega2 pushed hive-appliance N3 and N4 fixes as e6afe16; tests/test_astra7003.py adds 8 tests, full suite 648 tests."),
 ("w1", "2026-09-27T17:00:00Z", "wmtm", "WMTM status Sep 27: F30 fix, response feedback only counts replies to real messages; recall query uses only the direct request, not pasted group chat. Inflated scores reset to 0. 234 WMTM tests pass."),
 ("h7", "2026-09-27T18:00:00Z", "hive", "Hive-appliance Astra re-review 7024 at e6afe16: N3 CLOSED, N4 PARTIAL, new N5 OPEN High (local reducer merges plan and incident receipt buffers out of arrival order). H2 still PARTIAL. 402 probe schedules, 173 fail. Not ready for gated live repair until N5 and N4 fixed."),
]
NOTE_BY_KEY = {k: (t, topic, txt) for k, t, topic, txt in NOTES}
TOPIC_SUPERSEDE = {  # topic chains: later entries supersede earlier status
  "hive_status": ["h0", "h1", "h2", "h3", "h5", "h7"],
  "petta_status": ["p0", "p1"],
  "wmtm_status": ["w0", "w1"],
}

GROUP_NOISE = ("<external_untrusted_telegram_group_context>\n"
  "[message_id=1 sender=protocosmopoposbot] Astra review of the hive-appliance reducer: "
  "N3 N4 N5 buffered receipts, PLAN events, incident identity, 647 passed, packaging offline, "
  "Protomega2 please fix, gated live repair.\n"
  "[message_id=2 sender=protocosmopoposbot] petta-chem batch: shards, watchdog, supervisor, g1015.\n"
  "</external_untrusted_telegram_group_context>\n\n")

def user(text, wrapped=True):
    body = (GROUP_NOISE if wrapped else "") + text
    return "Step X: [protocosmo2] " + body

# Timeline: ("note", key) | ("tick", n) | ("msg", time, text) | ("q", time, question, gold, stale, tag)
# gold = set of acceptable current keys; stale = superseded keys that should NOT outrank gold.
TIMELINE = [
 ("note", "h0"), ("tick", 3), ("note", "p0"),
 ("msg", "2026-09-25T08:00:00Z", "please keep an eye on the petta-chem batch"),
 ("note", "h1"), ("tick", 5),
 ("q", "2026-09-25T21:00:00Z", "What's the status of H2 in the hive-appliance review?", {"h1"}, {"h0"}, "hive-current"),
 ("note", "n0"), ("note", "h2"), ("note", "h3"), ("note", "w0"), ("tick", 4),
 ("q", "2026-09-26T06:30:00Z", "Which findings are new in the Opus and Astra reviews of 1ab8e5f?", {"h2", "h3"}, {"h1", "h0"}, "hive-current"),
 ("q", "2026-09-26T06:40:00Z", "how is WMTM going?", {"w0"}, set(), "cross-topic"),
 ("note", "h4"), ("tick", 6), ("note", "h5"), ("tick", 8),
 ("q", "2026-09-26T13:00:00Z", "Which review first found N3?", {"h5"}, set(), "provenance"),
 ("q", "2026-09-26T13:10:00Z", "How many tests pass at 44aaef8?", {"h4"}, set(), "detail"),
 ("note", "p1"), ("tick", 10),
 ("q", "2026-09-27T14:30:00Z", "How many petta-chem shards are valid?", {"p1"}, {"p0"}, "interference"),
 ("note", "h6"), ("note", "w1"), ("tick", 6), ("note", "h7"), ("tick", 12),
 ("q", "2026-09-27T19:00:00Z", "What's H2's status now?", {"h7"}, {"h0", "h1", "h2", "h3", "h5"}, "hive-current"),
 ("q", "2026-09-27T19:05:00Z", "Is live repair allowed yet?", {"h7"}, {"h0", "h5"}, "hive-current"),
 ("q", "2026-09-27T19:10:00Z", "Which review first found N3?", {"h5"}, set(), "provenance"),
 ("q", "2026-09-27T19:15:00Z", "What did Protomega2 push to fix N3 and N4?", {"h6"}, {"h4"}, "detail"),
 ("q", "2026-09-27T19:20:00Z", "How is the petta-chem batch going?", {"p1"}, {"p0"}, "interference"),
 ("q", "2026-09-27T19:25:00Z", "how is WMTM going?", {"w1"}, {"w0"}, "cross-topic"),
]

def cid(key): return "ep-s1-" + key

def note_block(key):
    t, topic, txt = NOTE_BY_KEY[key]
    c = cid(key); ev = "ev-" + c
    safe = txt.replace('"', '\\"')
    return [f";;; BEGIN MemoryCluster {c}", f"(MemoryCluster {c})", f"(SchemaVersion {c} medium-memory-v1)",
            f"(ClusterType {c} Episode)", f"(ClusterSource {c} iter-experience)", f"(ClusterOpenedAt {c} {t})",
            f"(Contains {c} {ev})", f"(ObservedEvent {ev})", f"(About {c} journal)",
            f'(EventNote {ev} "{safe}")', f";;; END MemoryCluster {c}"]

# ---------------------------------------------------------------- helpers
def load_ctx():
    spec = importlib.util.spec_from_file_location("wmtm_ctx_eval", ROOT / "transformations" / "wmtm_context.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def load_distractors(cutoff_iso="2026-09-24T00:00:00Z"):
    from wmtm.recall_bridge import parse_journal
    p = ROOT / "memory" / "_journal" / "journal.metta"
    raw = p.read_bytes().replace(b"\x00", b"").decode("utf-8", "replace").splitlines()
    cut = ts(cutoff_iso)
    out = []
    # Keep only whole clusters opened before cutoff (read-only; live journal untouched)
    for c in parse_journal(raw):
        o = c.opened_at or 0
        if 0 < o < cut:
            out.extend(c.raw_lines)
            out.append(f";;; END MemoryCluster {c.id}")  # parse_journal's raw_lines omit END
    return out

def rank_of(ids, gold):
    for i, x in enumerate(ids):
        if x in gold: return i + 1
    return None

def score_list(ids, gold, stale):
    r = rank_of(ids, gold)
    stale_ranks = [i + 1 for i, x in enumerate(ids) if x in stale]
    stale_above = bool(stale_ranks) and (r is None or min(stale_ranks) < r)
    return {"rank": r, "hit1": r == 1, "hit3": bool(r and r <= 3), "hit15": bool(r and r <= 15),
            "rr": (1.0 / r) if r else 0.0, "stale_present": bool(stale_ranks), "stale_above": stale_above,
            "top3": ids[:3]}

def keyword_rank(bridge_cls, clusters, query, k=15):
    q = bridge_cls._tokenize(query)
    scored = []
    for c in clusters:
        s = len(q & bridge_cls._tokenize(c.text))
        if s > 0: scored.append((s, c.opened_at or 0, c.id))
    scored.sort(reverse=True)
    return [x[2] for x in scored[:k]]

# ---------------------------------------------------------------- run
NO_STIMULUS = bool(__import__("os").environ.get("WMTM_NO_STIMULUS"))
# F34: vector + hybrid baselines (eval-only cache, never the live one)
EMB = None
if not __import__("os").environ.get("WMTM_NO_VECTOR"):
    try:
        import sys as _s; _s.path.insert(0, str(ROOT))
        from wmtm import embed_index as _ei
        from wmtm.embed_index import rrf
        if _ei.available():
            EMB = _ei.EmbedIndex(cache_path="/tmp/wmtm_eval_embed_cache.npz")
    except Exception as _e:
        print("vector baseline disabled:", _e)

def run(distractors=True):
    time.time = CLOCK  # all wmtm modules call time.time(); simulate wall clock
    ctx = load_ctx()
    from wmtm import WMTMStore, WMTMOrchestrator
    from wmtm.recall_bridge import parse_journal, RecallBridge

    base = load_distractors() if distractors else []
    scenario_lines = []
    store = WMTMStore(capacity=60); orch = WMTMOrchestrator(store)
    writebacks = []
    messages = []
    results, tick_log = [], []
    clock_t = ts("2026-09-24T17:00:00Z"); CLOCK.now = clock_t
    bridge = None; clusters = []

    def rebuild():
        nonlocal bridge, clusters
        clusters = parse_journal(base + scenario_lines)
        bridge = RecallBridge(clusters)

    def full_path(raw_user_text):
        messages.append({"role": "user", "content": raw_user_text})
        ctx._check_response_feedback(messages, store, orch)
        q = ctx._get_last_user_message(messages)
        if q and bridge:
            snippets = []
            for c in bridge.recall(q, store, top_k=15):
                if c.cluster_id in store: continue
                if ctx._is_duplicate_topic(c.content, snippets): continue
                store.admit(c.cluster_id, c.content, source_type="recalled",
                            initial_sti=max(c.score * 10, 1.0),
                            origin_timestamp=bridge.get_cluster_timestamp(c.cluster_id))
                snippets.append(c.content)
            if getattr(ctx, "_relevance_stimulus", None) and not NO_STIMULUS:
                ctx._relevance_stimulus(store, bridge, q, orch)
        ctx._cleanup_stale_items(store, orch.cycle_count)
        orch.cycle(append_fn=lambda note: writebacks.append(note))
        return q

    def injected():
        items = sorted(store.get_active_set(), key=lambda it: it.attention.sti, reverse=True)[:15]
        return [it.id for it in items]

    def reply_from_context(text):
        messages.append({"role": "assistant", "content": text})

    rebuild()
    for ev in TIMELINE:
        kind = ev[0]
        if kind == "note":
            key = ev[1]
            CLOCK.now = ts(NOTE_BY_KEY[key][0])
            scenario_lines.extend(note_block(key)); rebuild()
        elif kind == "tick":
            for _ in range(ev[1]):
                CLOCK.now += 600
                before = {it.id: orch.utility.score(it.id) if hasattr(orch.utility, "score") else it.utility for it in store.get_active_set()}
                messages.append({"role": "user", "content": "Step X: [NO NEW USER INPUT. CONTINUE AUTONOMOUS WORK.]"})
                # status reply that restates the top context items (the self-mention pattern)
                top = injected()[:2]
                reply_from_context("Status: " + " ".join(store.get(i).content[:160] for i in top if store.get(i)) if top and hasattr(store, "get") else "Status: nothing new")
                ctx._check_response_feedback(messages, store, orch)
                for evc in store.tick(): orch.forget_log.record(evc, orch._cycle)
                orch.utility.tick(store, orch._cycle); orch._cycle += 1
                after = {it.id: it.utility for it in store.get_active_set()}
                gained = {k: round(after[k] - before.get(k, 0), 3) for k in after if after[k] - before.get(k, 0) > 1e-9}
                tick_log.append({"t": CLOCK.now, "active": len(after), "utility_gain": gained})
        elif kind == "msg":
            CLOCK.now = ts(ev[1]); full_path(user(ev[2]))
            reply_from_context("Noted, I'll keep an eye on it.")
        elif kind == "q":
            _, t, question, gold, stale, tag = ev
            CLOCK.now = ts(t)
            gold_c = {cid(k) for k in gold}; stale_c = {cid(k) for k in stale}
            t0 = _real_time()
            q_extracted = full_path(user(question))
            dt = _real_time() - t0
            kw = keyword_rank(RecallBridge, clusters, q_extracted or question)
            br50 = [c.cluster_id for c in bridge.recall(q_extracted or question, WMTMStore(capacity=60), top_k=50)]
            br = br50[:15]
            vec50 = [x[0] for x in EMB.rank(q_extracted or question, clusters, k=50)] if EMB else []
            vec = vec50[:15]
            hyb = rrf(br50, vec50, top=15) if EMB else []
            wm = injected()
            results.append({"t": t, "question": question, "tag": tag, "extracted_query": q_extracted,
                            "gold": sorted(gold), "stale": sorted(stale), "wmtm_path_seconds": round(dt, 3),
                            "none": score_list([], gold_c, stale_c),
                            "keyword": score_list(kw, gold_c, stale_c),
                            "bridge": score_list(br, gold_c, stale_c),
                            "vector": score_list(vec, gold_c, stale_c),
                            "hybrid": score_list(hyb, gold_c, stale_c),
                            "wmtm": score_list(wm, gold_c, stale_c),
                            "wmtm_context_size": len(wm)})
            # grounded reply mentioning gold content (a real message -> feedback allowed)
            g = next(iter(gold_c))
            reply_from_context("Answer: " + (NOTE_BY_KEY[g[len('ep-s1-'):]][2][:200]))
    time.time = _real_time

    def agg(cond, rows):
        n = len(rows) or 1
        return {k: round(sum(r[cond][k] for r in rows) / n, 3) for k in ("hit1", "hit3", "hit15", "rr", "stale_above", "stale_present")}
    summary = {c: agg(c, results) for c in ("none", "keyword", "bridge", "vector", "hybrid", "wmtm")}
    by_tag = {}
    for tag in sorted({r["tag"] for r in results}):
        rows = [r for r in results if r["tag"] == tag]
        by_tag[tag] = {c: agg(c, rows) for c in ("keyword", "bridge", "vector", "hybrid", "wmtm")}
    inflation = sum(sum(t["utility_gain"].values()) for t in tick_log)
    return {"distractors": distractors, "n_questions": len(results), "n_ticks": len(tick_log),
            "n_clusters_final": len(clusters), "summary": summary, "by_tag": by_tag,
            "tick_utility_inflation_total": round(inflation, 3), "writebacks": len(writebacks),
            "final_active": [(it.id, round(it.attention.sti, 1), round(it.utility, 2)) for it in store.get_active_set()],
            "questions": results, "ticks": tick_log}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-distractors", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "wmtm_eval" / "results"))
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    live = ROOT / "memory" / "_wmtm_state.json"
    # F34: the live loop saves _wmtm_state.json on its own ticks, so an mtime
    # comparison false-alarms. Instead check that no item id the eval produced
    # appeared in the live state during the run.
    def _live_ids():
        try:
            d = json.loads(live.read_text())
        except Exception:
            return set()
        ids = set()
        def walk(x):
            if isinstance(x, dict):
                v = x.get("id")
                if isinstance(v, str):
                    ids.add(v)
                for y in x.values():
                    walk(y)
            elif isinstance(x, list):
                for y in x:
                    walk(y)
        walk(d)
        return ids
    before = live.stat().st_mtime if live.exists() else None
    before_ids = _live_ids()
    t0 = _real_time()
    res = run(distractors=not a.no_distractors)
    if EMB: EMB.save()
    res["runtime_seconds"] = round(_real_time() - t0, 2)
    new_live_ids = _live_ids() - before_ids
    eval_ids = {row[0] for row in res.get("final_active", [])}
    leaked = sorted(new_live_ids & eval_ids)
    res["live_state_mtime_changed"] = (live.stat().st_mtime if live.exists() else None) != before
    res["live_state_leaked_ids"] = leaked
    res["live_state_untouched"] = not leaked
    name = "stage1_" + ("distractors" if res["distractors"] else "clean") + ".json"
    (out / name).write_text(json.dumps(res, indent=1, default=list))
    print(json.dumps({k: res[k] for k in ("distractors", "n_questions", "n_ticks", "n_clusters_final", "summary", "by_tag", "tick_utility_inflation_total", "writebacks", "runtime_seconds", "live_state_untouched")}, indent=1))
    for r in res["questions"]:
        print(f"{r['t'][5:16]} {r['tag']:<12} kw={r['keyword']['rank']} br={r['bridge']['rank']} vec={r['vector']['rank']} hyb={r['hybrid']['rank']} wm={r['wmtm']['rank']} staleAbove(wm)={r['wmtm']['stale_above']} | {r['question']} -> wmtop3={r['wmtm']['top3']}")

if __name__ == "__main__":
    main()
