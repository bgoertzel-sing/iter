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
