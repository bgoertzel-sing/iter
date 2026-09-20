# Goal-Driven Iter Control: Revised Design Proposal

**Subsystems:** Iter, WMTM, PLN, GoalChainer  
**Date:** 2026-09-19  
**Status:** Proposal for implementation and adversarial review; **not approved for live control**  
**Supersedes:** the control architecture in goal_driven_design_spec.md (SHA-256 7bd2bc1515b715a47189379ebb34d130ca61e43bdce28d0e02e6a7718a98d4bf)  
**Review basis:** GPT-6 Astra architecture review recorded in projects/petta-memory/NOTES.md

## 1. Decision and scope
Build a bounded, auditable goal-pursuit system in which symbolic and learned components may propose or appraise actions but cannot authorize effects. The first implementation target is a deterministic domain in shadow mode. Live mutation remains disabled until successive evidence gates pass.

Eight roles: Goal management, Observation, PLN beliefs, Planning (typed operators), GoalChainer appraisal, Authorization (reference monitor), Execution (dispatcher), Evaluation/learning.

## 2. Safety invariants (I1-I10)
I1: No grant, no effect. I2: Obligation != authorization. I3: Exact scope. I4: Version closure. I5: Independent completion. I6: At-most-once + reconcile. I7: No memory-to-actuation. I8: Learning cannot rewrite authority. I9: Human stop dominates. I10: Fail closed.

## 3-14: Component architecture, typed domain model, transactional state, control cycle, WMTM learning policy, bounded domain, verification, staged gates (1-10), work packages (1-10), finding resolution table, non-goals, open questions.

Full spec preserved from protocosmopoposbot revised design (message 11331).
