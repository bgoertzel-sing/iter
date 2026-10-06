"""PeTTa string-literal codec tests (I1). Mirrors PeTTa src/parser.pl string_lit."""
import json, os, shutil, subprocess, tempfile, pytest
from wmtm.metta_codec import encode_string, decode_string

CASES = ['plain', 'say "hi"', 'back\\slash', 'C:\\new\\table', 'unicode é中😀', 'literal \\u00e9 text', 'tab\there\nnew\rcr', 'trailing \\', '"', '\\"', 'paren ( ) $x', '', 'a\\\\b', 'emoji 🎵 "q" \\n literal']

@pytest.mark.parametrize("s", CASES)
def test_roundtrip_python_mirror(s):
    assert decode_string(encode_string(s)) == s

def test_old_json_dumps_is_broken():
    bad = [s for s in CASES if decode_string(json.dumps(s, ensure_ascii=True)) != s]
    assert bad, "json.dumps(ensure_ascii=True) should corrupt non-ASCII under PeTTa rules"

def test_no_unicode_escapes_emitted():
    assert "\\u" not in encode_string("\u00e9\u4e2d")

def test_nul_rejected():
    with pytest.raises(ValueError):
        encode_string("a\x00b")

PARSER = os.environ.get("PETTA_PARSER")
SWIPL = os.environ.get("SWIPL", shutil.which("swipl") or "")

@pytest.mark.skipif(not (PARSER and SWIPL), reason="set PETTA_PARSER and SWIPL to run against real PeTTa sread")
def test_roundtrip_real_petta_sread():
    d = tempfile.mkdtemp()
    enc = os.path.join(d, "enc.txt")
    with open(enc, "w", encoding="utf-8") as f:
        for s in CASES: f.write(encode_string(s) + "\n")
    pl = os.path.join(d, "rt.pl")
    open(pl, "w").write(f""":- consult('{PARSER}').
:- set_stream(user_output, encoding(utf8)).
loop(S) :- read_line_to_string(S,L), ( L == end_of_file -> true ; sread(L,T), string_codes(T,C), format("~w~n",[C]), loop(S) ).
:- initialization((open('{enc}',read,S,[encoding(utf8)]), loop(S), halt)).
""")
    out = subprocess.run([SWIPL, "-q", pl], capture_output=True, text=True, timeout=60).stdout.splitlines()
    got = ["".join(map(chr, eval(l))) for l in out]
    assert got == CASES

