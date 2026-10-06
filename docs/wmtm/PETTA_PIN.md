# PeTTa pin for I1 (Ben, Oct 6 2026: "Just upgrade petta ofc")

- Omega c6dc842 Dockerfile pins `ARG PETTA_REF=v1.0.4` (lacks 21be21f swrite backslash fix).
- I1 builds with `--build-arg PETTA_REF=v1.0.5` (df313cd, 2026-07-23). v1.0.5 contains 21be21f (+ #200 follow-up d9a437c).
- v1.0.4..v1.0.5 = 14 commits (swrite fix, import overhaul, commit-pinned git imports, overapplication error, specializer fix).

Verified Oct 6 (local swipl 9.3.36, wmtm codec cases, 14 strings):
- v1.0.4 sread->swrite->sread round trip: 7/14 OK (all backslash cases broken).
- v1.0.5 round trip: 14/14 OK. sread on codec encodings unchanged.
- v1.0.5 examples atomops, and_or, add_atom_fun_space pass; `repr` keeps backslashes.

`petta_parser_swrite_backslash.patch` is now OBSOLETE (kept only for history; do not apply on v1.0.5).
Not yet run: full Omega c6dc842 boot on v1.0.5 (import overhaul is the main risk).

## Oct 6 boot check (Omega c6dc842 on PeTTa v1.0.5)
- lib_omegaclaw.metta loads to completion (rc=0, sentinel printed) on v1.0.5 + swipl 9.3.36; no new errors from the import overhaul.
- CORRECTION: check_metta_imports.py only matched `(library Omega ...)`, but c6dc842 imports via `(library OmegaClaw-Core ...)`. Regex fixed. With it, c6dc842 lib_omegaclaw.metta:26 `./src/context` is MISSING (file never existed in Omega history), so c6dc842 also silently skips context. Not a v1.0.5 regression.
- Not covered: git-import of petta_lib_chromadb, Python py-call runtime, full agent loop.
