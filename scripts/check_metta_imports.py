#!/usr/bin/env python3
"""I1 loud-import check. PeTTa 'import!' wraps importer_helper in catch(_,fail),
so a missing module is skipped silently. This resolves every (library Omega ./x)
import in a .metta file the way importer_helper does (.py as-is, else +.metta)
and exits non-zero listing anything missing."""
import re, sys, pathlib
PAT = re.compile(r'\(import!\s+&self\s+\(library\s+(?:Omega|OmegaClaw-Core)\s+(\./[^\s)]+)\)\)')
def check(lib):
    lib = pathlib.Path(lib); root = lib.parent; missing = []
    for n, line in enumerate(lib.read_text(encoding='utf-8').splitlines(), 1):
        if line.lstrip().startswith(';'): continue
        for rel in PAT.findall(line):
            p = root / rel[2:]
            cands = [p] if p.suffix == '.py' else [p.with_name(p.name + '.metta')]
            if not any(c.is_file() for c in cands): missing.append((n, rel))
    return missing
if __name__ == '__main__':
    bad = 0
    for f in sys.argv[1:] or ['lib_omega.metta']:
        for n, rel in check(f):
            bad += 1; print(f'MISSING {f}:{n} {rel}', file=sys.stderr)
    print(f'import check: {bad} missing'); sys.exit(1 if bad else 0)
