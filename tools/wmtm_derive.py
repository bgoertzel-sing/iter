"""Register a derived belief in the WMTM (Working Medium-Term Memory).

When the agent draws a new conclusion from recalled facts, it should
register the derivation here so WMTM can track it, boost its attention
when useful, and promote it to LTM if it proves durable.

Uses file-based IPC: writes to memory/_wmtm_pending_derives.jsonl
which the wmtm_context transformation picks up on the next cycle.
"""
import json, hashlib
from pathlib import Path

DESCRIPTION = "Register a new derived belief in Working Memory. Arg: note (string) describing the conclusion. Optional: source_ids (list of str, the memory IDs this was derived from)."

def run(note, source_ids=None):
    """Register a derived belief in WMTM via file-based IPC."""
    try:
        if source_ids is None:
            source_ids = []
        elif isinstance(source_ids, str):
            source_ids = [source_ids]
        
        pending_path = Path(__file__).resolve().parent.parent / 'memory' / '_wmtm_pending_derives.jsonl'
        entry = json.dumps({'note': note, 'source_ids': source_ids})
        
        with open(pending_path, 'a') as f:
            f.write(entry + chr(10))
        
        item_id = 'derived-' + hashlib.md5(note.encode()).hexdigest()[:12]
        return 'Registered derived belief ' + item_id + ' in WMTM (pending): ' + note[:100]
    except Exception as e:
        return 'WMTM derive error: ' + str(e)
