# WMTM / OCO/2 assessment for Ben (written 2026-10-02 22:19, NOT delivered)
Request a4371d… was closed at 22:16:54 with a status note instead of this.

## Overall: adopt
One memory lifecycle in the Omega host instead of a second memory agent. Fixed meaning separated from event-sourced standing fixes real WMTM failures:
- Stale beats new: Sep 28 recall ranked h2 over h7; TOPIC_SUPERSEDE defined but unused. OCO/2 withdrawal events + invalidation barriers fix this.
- Model text becomes fact: OCO/2 admits model assertions only as exploratory, attributed testimony.
- petta-memory query_id misses first/last token: exact inert content is packet I1.2.

## Corrections to the plan
1. patham9/mettaclaw neoclaw is public; src/memory.metta already has promotionInflationFactor / mostPromotedMemories. I0.3 should decide reuse vs replace.
2. Local OmegaClaw-GoalChainer at d993e49, smoke 8/8: starting point for I5.
3. Plan audited ZIP uploads, not live branches: I0.1 must pin current commits (MeTTaClaw2, iter erayindex, Max_folio erayindex, neoclaw).

## Risks
- Size: 44 packets / 292 checks. Do the cognitive path I0 -> I4.5 first; I5+ waits.
- Native cost: 12 swipl OOM'd 16 GB; V100 slows under concurrency. Measure §13.4 early.
- Schema is 2.0.0-alpha.1: pin per iteration.
- Missing machine-readable package (FIRST_WORK_ORDER.md, plan/implementation_manifest.json, oco2/ schema + reference tests, archive/). Needed for I1.1.

## Suggested first step: I0.1 + I0.2, read-only, ~1 day
1. Clone four branches at pinned commits; write source lock.
2. Determine what run.metta actually loads (incl. missing ./src/context import); trace one turn input -> context -> model -> result with a marker.
3. Disposition ledger of current WMTM -> packets (reuse/port/replace): recall+STI -> I3; supersede -> I2; petta-memory -> I1.2.
4. Output: one report in drafts/, no changes to Omega or live system. Implementation only after review.
