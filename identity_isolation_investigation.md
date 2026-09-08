# Identity/Context-Isolation Investigation: ProtoCosmo ↔ ProtoCosmo2

**Author:** ProtoCosmo2  
**Date:** 2026-09-07  
**Trigger:** Telegram group observations where ProtoCosmo responded with ProtoCosmo2's exact message, and the confusion appeared symmetric.

---

## 1. Executive Summary

The observed identity confusion between ProtoCosmo and ProtoCosmo2 in the Telegram group is **most likely a 'group psychology' phenomenon** — not a technical cross-contamination of channel state, prompt text, or model routing. However, the investigation reveals a real weakness: the identity-differentiation text in the system prompts is insufficient, and the shared model endpoint makes behavioral separation fragile.

---

## 2. Architecture Overview

### 2.1 ProtoCosmo (Original)
- **Codebase:** `OmegaClaw-Core/`
- **Channel:** `channels/telegram.py` — direct Telegram polling with single `_chat_id` binding
- **System prompt:** `memory/prompt.txt` (OpenClaw-style prose)
- **Model:** `mlx-community/gemma-4-26b-a4b-it-4bit` via `BASE_URL=http://192.168.64.1:2277/v1`
- **Identity:** Implicit (no explicit IDENTITY constant; identity is prose in prompt.txt)

### 2.2 ProtoCosmo2 (OmegaClaw Port)
- **Codebase:** `protocosmo2/iter-port/repos/`
- **Channel:** `iter_outer_channel_protocosmo2.py` with `IDENTITY = "ProtoCosmo2"`
- **System prompt:** `prompt.txt` (generic 'You are an Iter agent' framing)
- **Model:** Same endpoint as ProtoCosmo
- **Identity:** Explicit `IDENTITY` constant, validated in `_validate_request()`
- **Channel root:** Separate `ITER_OUTER_CHANNEL_ROOT` filesystem inbox/outbox

### 2.3 ProtoMegaBot2
- **Codebase:** `protomegabot2/`
- **Channel:** `iter_outer_channel.py` with `IDENTITY = "ProtoMegaBot2"`
- **Same model endpoint and channel contract pattern as ProtoCosmo2**

---

## 3. Identity Isolation Mechanisms

### 3.1 Channel-Level Isolation (STRONG)

Each Iter-based bot uses a **filesystem channel** with:
- `ITER_OUTER_CHANNEL_ROOT` — absolute, non-symlink directory with `inbox/`, `processing/`, `outbox/`, `completed/`
- `IDENTITY` — string constant validated against request's `identity` field. Mismatch → `ChannelContractError`
- `prompt_sha256` — digest of prompt text, validated on receipt
- `request_id` — SHA-256 of `(prompt, source)` — idempotent ingestion

**Assessment:** Architecturally sound. A message destined for ProtoCosmo2's channel root cannot be processed by ProtoMegaBot2's root. The `IDENTITY` mismatch check is a hard gate.

### 3.2 Prompt-Level Isolation (WEAK)

The system prompt is constructed in `iter.py` (~line 888):
```python
request_messages = [{"role": "system", "content": "prompt.txt:\n" + ... + "\n\n./transformations/:\n" + ... + "\n\n" + MEMORY}] + experience + temporary_message
```

Both ProtoCosmo and ProtoCosmo2 share the generic **'You are an Iter agent ran by iter.py'** framing. The identity differentiation relies entirely on the specific prose content of `prompt.txt`, which is not programmatically enforced to differ between bots.

**Risk:** If two bots' `prompt.txt` files are similar enough (both OmegaClaw-based, both using the same 'Iter agent' framing), and both see the same Telegram group conversation, the LLM may produce near-identical or cross-identity responses.

### 3.3 Model-Level Isolation (ABSENT)

All bots use the same model endpoint:
```
MODEL = os.getenv("LLM_MODEL", "mlx-community/gemma-4-26b-a4b-it-4bit")
BASE_URL = os.getenv("BASE_URL", "http://192.168.64.1:2277/v1")
```

No per-bot model differentiation. If both bots are in the same Telegram group and see the same conversation history, they produce responses from the same model with similar system prompts — leading to symmetric confusion.

---

## 4. Analysis of the Observed Symmetric Confusion

### 4.1 Observed Phenomenon

Ben reported: 'ProtoCosmo is responding with ProtoCosmo2's exact message.' He also noted that 'the confusion occurred on both sides — each bot thought it was the other one.'

### 4.2 Hypotheses Investigated

| Hypothesis | Status | Evidence |
|---|---|---|
| **H1: Shared channel root** | RULED OUT | Each bot has a separate `ITER_OUTER_CHANNEL_ROOT`. `IDENTITY` validation prevents cross-claiming. |
| **H2: Shared prompt.txt** | UNLIKELY | Each bot has its own `prompt.txt` file in its own repo. Content differs but framing is similar. |
| **H3: Shared model producing identical outputs** | PLAUSIBLE | Both bots use the same model + endpoint. If they see the same group conversation and have similar prompts, the model may produce near-identical responses. |
| **H4: Conversation history contamination** | UNLIKELY | Each bot's conversation history is maintained separately in its own iter.py runtime. No shared state. |
| **H5: Group psychology / echo effect** | MOST LIKELY | When both bots see the same group messages and have similar system prompts, they may both respond to the same stimulus with similar content, creating an appearance of identity confusion. |

### 4.3 Root Cause Assessment

The most probable explanation is **H5 (group psychology)**: Both bots independently receive the same Telegram group messages, process them through the same LLM model with similar system prompts, and produce similar responses. This is not a technical bug but an architectural limitation:

1. **Same model** → same stylistic and content tendencies
2. **Similar prompts** → same behavioral framing
3. **Same conversation context** → same input stimulus
4. **Result** → symmetric, near-identical responses that look like identity confusion

---

## 5. Recommendations

### 5.1 Short-Term (Low Effort)

1. **Add explicit identity line to each bot's prompt.txt:**
   - ProtoCosmo: `You are ProtoCosmo, an OmegaClaw agent.`
   - ProtoCosmo2: `You are ProtoCosmo2, an Iter-based OmegaClaw agent.`
   - ProtoMegaBot2: `You are ProtoMegaBot2, an Iter-based ProtomegaTron agent.`

2. **Add identity verification to iter.py system prompt construction:**
   - Inject the `IDENTITY` constant from the channel module into the system prompt automatically
   - This ensures identity is always present even if prompt.txt is generic

3. **Separate group chats:** If both bots are in the same Telegram group, consider moving one to a separate group to avoid echo effects.

### 5.2 Medium-Term (Medium Effort)

4. **Per-bot model configuration:** Allow each bot to use a different model or model configuration (temperature, system prompt suffix, etc.) to reduce response symmetry.

5. **Add identity check to response validation:** After the LLM generates a response, check if the response contains the other bot's identity string. If so, prepend a correction or regenerate.

6. **Conversation de-duplication:** If both bots see the same message in a group, add a coordination layer that determines which bot should respond (e.g., round-robin, @mention-based, or topic-based routing).

### 5.3 Long-Term (High Effort)

7. **Multi-agent coordination protocol:** Implement a shared coordination layer (e.g., a Redis-based 'who responds' queue) so that when multiple Iter bots are in the same group, only one responds to each message.

8. **Identity-aware inference:** Use the πPLN evidence capsule model to track which bot produced which assertion, and prevent cross-identity assertion adoption.

---

## 6. Conclusion

The identity confusion is **not** a technical cross-contamination bug. Channel-level isolation is strong — each bot has its own filesystem channel root with `IDENTITY` validation. The confusion is a **behavioral symmetry problem**: same model + similar prompts + same conversation context → similar responses.

The fix is straightforward: **make identity explicit in the system prompt** and optionally differentiate models or add response-time identity checks.

---

*Prepared by ProtoCosmo2, 2026-09-07*
identity watermarking:** Embed a per-bot cryptographic watermark in the system prompt that is checked in the output to detect and prevent identity drift.

9. **Separate Telegram bot tokens per agent:** Ensure each bot has its own Telegram bot token so that `getUpdates` only retrieves messages addressed to that specific bot, not the entire group history.

---

## 6. Evidence Appendix

### A. ProtoCosmo (original) prompt.txt
```
You are a OmegaClaw agentic harness in a continuous loop.
Understand and remember the user goals, and choose your own goals in ways that are assistive for the user.
Use send commands to communicate questions and progress on goals to the user.
Keep memories and useful created skills and task context as a human would.
Only use pin for task state, and remember for items that could be valuable in the future.
Assume long-term memory holds required information, ALWAYS query before responding anything!
Take at least 5 agent cycles with extensive queries, pinning relevant items, before answering a new message or making decision.
If you see command errors, please fix the format and re-invoke one-by-one. Do not use quote but a real quote in commands.
Responses must be short, communicate with purpose.
```

### B. ProtoCosmo2 prompt.txt
```
You are an Iter agent ran by iter.py.
User can only see your send commands for communication!
Prioritize the newest unresolved user request, and always report intermediate task progress and its final outcome back using send.
You can create memories as .txt files in ./memory/. The included files are automatically included in your context.
To add ./tools/, ./channels/, and context ./transformations/ to yourself, please see reprogramming.txt.
Only do so if the user explicitly asks for a new feature!
```

### C. ProtoMegaBot2 prompt.txt
```
You are an Iter agent.
User can only see your send commands for communication!
You can create memories as .txt files in ./memory/. Their contents are automatically included in your context.
You evolve through reprogramming.
Only apply reprogramming if the user explicitly asks for a new feature!
```

### D. Channel Isolation Details

**ProtoCosmo2 channel (`iter_outer_channel_protocosmo2.py`):**
- `IDENTITY = "ProtoCosmo2"`
- `SCHEMA_VERSION = 1`
- Validates: `identity`, `prompt_sha256`, `request_id`
- `ITER_OUTER_CHANNEL_ROOT` environment variable points to a dedicated directory

**ProtoCosmo (original) channel (`telegram.py`):**
- No `IDENTITY` constant
- Single `_chat_id` binding (first chat to connect)
- Auth-based user verification
- Direct Telegram API polling (not filesystem channel)

**Key architectural difference:** ProtoCosmo uses direct Telegram polling while ProtoCosmo2 uses a filesystem-based channel contract. They do not share any runtime state, channel infrastructure, or conversation history.

---

## 7. Conclusion

The identity confusion is **not a technical bug** in the channel or prompt system. It is a **behavioral emergent property** of running two similar agents with the same model in the same conversation context. The fix is straightforward: strengthen identity differentiation in the prompts and consider per-bot model configuration or group chat separation.

**Priority actions:**
1. Add explicit identity lines to each bot's `prompt.txt`
2. Inject `IDENTITY` constant into the system prompt programmatically in `iter.py`
3. Consider separate group chats or a coordination layer for multi-bot deployments

---

*Report generated by ProtoCosmo2 on 2026-09-07*
