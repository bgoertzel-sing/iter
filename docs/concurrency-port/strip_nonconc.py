"""Strip non-concurrency features from a ProtoCosmo2 iter.py snapshot.

usage: strip_nonconc.py IN OUT [--model-tag]
Prints one line per rule: name, number of replacements.  Rules that do not
apply to a snapshot report 0; the caller checks the expected counts.
"""
import re
import sys

src_path, out_path = sys.argv[1], sys.argv[2]
model_tag = "--model-tag" in sys.argv
s = open(src_path, encoding="utf-8").read()
log = []


def sub(name, old, new, regex=False, flags=0):
    global s
    if regex:
        s, n = re.subn(old, new, s, flags=flags)
    else:
        n = s.count(old)
        s = s.replace(old, new)
    log.append((name, n))


# E1 PDF turn images (2026-09-29)
sub("pdf_block", r"\n# BEGIN PDF_TURN_IMAGES.*?# END PDF_TURN_IMAGES\n\n", "", regex=True, flags=re.S)
sub("pdf_call", "            request_messages = attach_turn_pdf_images(request_messages)\n", "")

# E2 MAX_TOKENS 2524 -> 16000 -> 32000 (2026-09-25/26)
sub("max_tokens", "MAX_TOKENS = 32000\n", "MAX_TOKENS = 2524\n")

# E3 tool_choice required->auto (09-22) and back (10-05): restore base value "required"
sub("tool_choice_kw", 'tool_choice="auto"', 'tool_choice="required"')
sub("tool_choice_pos", 'request_tools, "auto", MAX_TOKENS', 'request_tools, "required", MAX_TOKENS')

# E4 docstrings commit dc1d872 (non-concurrency functions only; BranchState.__init__ kept)
for line in [
    '    """Execute a dynamic tool function in a subprocess and write JSON result."""\n',
    '    """Invoke *function* from the module at *path* in a subprocess, returning its JSON result."""\n',
    '    """Return the current wall-clock time as a formatted string."""\n',
    '    """Poll all channel modules for inbound events and return them as a joined string."""\n',
    '    """Sleep up to SLOW_STEP_DELAY seconds, checking for input each second."""\n',
    '    """Atomically write the experience list to experience.json."""\n',
    '    """Load tool metadata from tools/*.py, returning (inops, overflow_count, errors)."""\n',
    '    """Convert loaded tool metadata into OpenAI function-calling tool definitions."""\n',
    '    """Load DESCRIPTION constants from transformations/*.py and return as a joined string."""\n',
    '    """Apply all transformation modules to (messages, tools) and return updated values plus errors."""\n',
]:
    sub("docstring", line, "")

# E5 957a469 invoke_dynamic positional-only path
sub("invoke_posonly", "def invoke_dynamic(path, /, function,", "def invoke_dynamic(path, function,")

# E6 1519bf1 DYNAMIC_TIMEOUT 5 -> 30
sub("dynamic_timeout", "DYNAMIC_TIMEOUT = 30\n", "DYNAMIC_TIMEOUT = 5\n")

# E7 terminal provider breaker 2727dbe + 87374c3
sub("terminal_funcs",
    r"TERMINAL_PROVIDER_STATUS_CODES = frozenset\(\{400, 401, 402, 403, 413\}\)\n\n"
    r"def terminal_provider_status\(error\):.*?\n"
    r"def send_terminal_provider_failure\(error\):.*?"
    r"    print\(f\"TERMINAL_PROVIDER_BREAKER_SENT: status=\{status\}\"\)\n    return True\n\n",
    "", regex=True, flags=re.S)
sub("terminal_turn_checkpoint", "    turn_checkpoint = len(experience)\n", "")
sub("terminal_except",
    r"        if send_terminal_provider_failure\(error\):\n"
    r"            experience = experience\[:turn_checkpoint\]\n"
    r"(?:            .*\n)*?"
    r"            continue\n",
    "", regex=True)

if model_tag:
    # E8 model tag c234e28 (2026-09-30)
    sub("mt_block", r"\n# BEGIN MODEL_TAG.*?# END MODEL_TAG\n", "", regex=True, flags=re.S)
    sub("mt_capped", "        return _tracked_llm_create(llm_client, **kwargs)\n",
        "        return llm_client.chat.completions.create(**kwargs)\n")
    sub("mt_fg_create", "response = _tracked_llm_create(client, model=MODEL,",
        "response = client.chat.completions.create(model=MODEL,")
    sub("mt_cp_sig", 'def send_checkpoint_message(checkpoint_data, checkpoint_path, model_tag=""):',
        "def send_checkpoint_message(checkpoint_data, checkpoint_path):")
    sub("mt_cp_body",
        "message = apply_model_tag(format_checkpoint_message(checkpoint_data, checkpoint_path), model_tag)",
        "message = format_checkpoint_message(checkpoint_data, checkpoint_path)")
    sub("mt_fallback_sig", 'def send_text_only_fallback(content, model_tag=""):',
        "def send_text_only_fallback(content):")
    sub("mt_fallback_body", "    text = apply_model_tag(text, model_tag)\n", "")
    sub("mt_progress_sig", 'def send_progress_note(content, model_tag=""):',
        "def send_progress_note(content):")
    sub("mt_progress_body",
        "    text = apply_model_tag(content, model_tag)\n    try:\n"
        "        result = invoke_dynamic(Path(\"tools/send.py\"), \"run\", channel=CHECKPOINT_CHANNEL, content=text, final=False)",
        "    try:\n"
        "        result = invoke_dynamic(Path(\"tools/send.py\"), \"run\", channel=CHECKPOINT_CHANNEL, content=content, final=False)")
    sub("mt_tag_send_args", "**tag_send_arguments(tool_name, tool_arguments, response))", "**tool_arguments)")
    sub("mt_fallback_calls", ", served_model_tag(response))", ")")
    sub("mt_branch_cp", '        checkpoint_data["_model_tag"] = served_model_tag(response)\n', "")
    sub("mt_drain_filter", '"request_ids", "_model_tag")}', '"request_ids")}')
    sub("mt_drain_send", 'send_checkpoint_message, cp_data, cp_path, cp.get("_model_tag", ""))',
        "send_checkpoint_message, cp_data, cp_path)")

open(out_path, "w", encoding="utf-8").write(s)
for name, n in log:
    print(f"{name}\t{n}")
leftover = [w for w in ("attach_turn_pdf", "PDF_TURN", "terminal_provider", "turn_checkpoint",
                        "_tracked_llm_create", "served_model", "model_tag", "MODEL_TAG")
            if w in s]
print("LEFTOVER:", leftover)
