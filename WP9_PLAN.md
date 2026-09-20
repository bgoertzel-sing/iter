# WP9: End-to-End Integration Test

## Goal
Create a single test that exercises the full pipeline:
  WMTM (observe) → PLN (infer beliefs) → GoalChainer (plan) → Validate → Authorize → Dispatch → Evaluate → Writeback

## Steps
1. Create test_wp9_e2e.py with a realistic scenario:
   - Seed WMTM with several facts (CPU, memory, disk metrics)
   - Set up a GoalSpec ("reduce CPU load")
   - Create integrated orchestrator with use_pln=True
   - Register ActionOperator for goalchainer_decision
   - Run multiple cycles
   - Assert: observations produced, beliefs inferred, plans generated, validated, dispatched (shadow), evaluated, written back to WMTM

2. Test writeback path: after dispatch, effects should produce new WMTM items

3. Test goal satisfaction: when evaluate returns SATISFIED, orchestrator stops or marks goal done

4. Test error handling: if PLN or GC fails, cycle continues gracefully

## Success Criteria
- All WP9 tests pass
- Total tests: 480 + N
- Full pipeline exercised in at least one test
