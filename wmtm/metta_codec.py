"""PeTTa-safe MeTTa string-literal codec for I1.
PeTTa parser (src/parser.pl string_chars): inside "...", a backslash takes the next
char literally except n/t/r -> LF/TAB/CR. Everything else (incl. UTF-8) passes raw.
So the only safe encoding is: escape backslash and quote, map LF/TAB/CR, keep UTF-8 raw.
json.dumps(ensure_ascii=True) is WRONG: it emits \\uXXXX which PeTTa reads as 'uXXXX'."""
_MAP = {'\\': '\\\\', '"': '\\"', '\n': '\\n', '\t': '\\t', '\r': '\\r'}

def encode_string(s: str) -> str:
    if not isinstance(s, str):
        raise TypeError('encode_string expects str')
    if '\x00' in s:
        raise ValueError('NUL not representable in PeTTa string literal')
    return '"' + ''.join(_MAP.get(c, c) for c in s) + '"'

def decode_string(lit: str) -> str:
    """Python mirror of PeTTa string_lit, for tests."""
    if len(lit) < 2 or lit[0] != '"' or lit[-1] != '"':
        raise ValueError('not a string literal')
    out, i, body = [], 0, lit[1:-1]
    while i < len(body):
        c = body[i]
        if c == '\\':
            if i + 1 >= len(body):
                raise ValueError('dangling backslash')
            x = body[i + 1]; out.append({'n': '\n', 't': '\t', 'r': '\r'}.get(x, x)); i += 2
        elif c == '"':
            raise ValueError('unescaped quote')
        else:
            out.append(c); i += 1
    return ''.join(out)
