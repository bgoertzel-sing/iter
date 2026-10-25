# Experiment sanity gate

Origin: glicerico, 2026-10-13. Lesson: petta-chem neutral calibration v1/v2 (about two months of compute) had an unevaluated maximal-RAF field in every row (missing import; PeTTa silently keeps unevaluated calls as data). The smoke test passed because it imported an extra file. Exit codes, receipts, hashes and manifests were all green. Green bookkeeping is not evidence the science ran.

Apply before building, launching or trusting any experiment, batch, benchmark or long run, and copy these requirements into any subagent or worker brief.

1. Known-answer canary: run at least one input whose correct output is independently knowable through the EXACT production path (same generated inputs, imports, runner and command). Never use a harness that adds its own imports or setup. Confirm the output is what you expect and fits the intended use.
2. Per-unit structural check: wire a check into the completion step that refuses to record a result unless it is well-formed: no unevaluated function calls left as data, required fields present, values in range, logically consistent (e.g. "reachable implies non-empty RAF"). On failure stop that unit and report it; never skip it silently.
3. Failure-mode statement: for each output field, write one line in the plan: what it should look like, and what observable would reveal a silent failure.
4. Pilot before long runs: for runs longer than about a day, finish 1-2 units first and review wall time and well-formedness before scaling. Runtime estimates from runs where a step never executed are void.
5. Ledger: record which checks ran, what was expected and what was observed. A batch lacking these is invalid regardless of exit code.

Structural checks are process checks, not outcome inspection; blinding rules never forbid them.

Prompt rules alone are not enforcement: put checks 1-2 in code. Reference implementation: petta-chem scripts/validate_neutral_calibration_row.py (neutral calibration v3), which runs before each receipt is written and freezes the seed on failure.
