"""Shared embedding-provider client for Iter's PeTTa-memory tools (remember.py, chroma_query.py).

Replaces the upstream `omega`-branch default of OpenAI `text-embedding-3-large`
with SingularityCompute's hosted embedding models, reusing the same API key
already provisioned for chat completions (AI_API_KEY).

Why this exists (2026-09-14 migration note):
  - Upstream tools/remember.py and tools/chroma_query.py call OpenAI() with no
    base_url override, so they require OPENAI_API_KEY. No such key is
    provisioned anywhere in this hive (protomega/protomega2 use Anthropic /
    OpenRouter / SingularityCompute for chat) -- so as shipped, these tools
    do not work at all.
  - SingularityCompute (https://llm.a.singularitycompute.com/v1) exposes an
    OpenAI-compatible /v1/embeddings endpoint and currently serves two
    loaded embedding models: WhereIsAI/UAE-Large-V1 (335M, 1024-dim,
    stronger retrieval quality) and BAAI/bge-base-en-v1.5 (109M, 768-dim,
    smaller/faster). Both report a 512-token context window.
  - Verified live 2026-09-14: the endpoint does NOT error on over-length
    input -- it silently truncates and still returns HTTP 200. The response
    `usage.prompt_tokens` field was also observed to NOT reliably reflect
    the actual token count the embedding model saw (it stayed inflated even
    for short repeated-phrase inputs), so it cannot be used as a truncation
    signal. Conclusion: chunking MUST happen client-side, proactively,
    before the request is sent -- there is no server-side signal to react to
    after the fact.
"""
from __future__ import annotations

import os
import re
import uuid

DEFAULT_BASE_URL = "https://llm.a.singularitycompute.com/v1"
DEFAULT_MODEL = "WhereIsAI/UAE-Large-V1"

# Conservative chunk budget: the model's stated context is 512 tokens, but
# since the server silently truncates rather than erroring, we stay well
# under it. ~4 chars/token is a conservative estimate for English prose with
# a wordpiece/BPE-style tokenizer (this deliberately over-estimates token
# count per char to stay safe, not under-estimates).
CHARS_PER_TOKEN_ESTIMATE = 4
MAX_TOKENS_PER_CHUNK = 400
MAX_CHARS_PER_CHUNK = MAX_TOKENS_PER_CHUNK * CHARS_PER_TOKEN_ESTIMATE  # 1600

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


def _client():
    from openai import OpenAI
    base_url = os.environ.get("EMBEDDING_BASE_URL", DEFAULT_BASE_URL)
    api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("AI_API_KEY")
    if not api_key:
        raise RuntimeError("No embedding API key: set EMBEDDING_API_KEY or AI_API_KEY")
    return OpenAI(base_url=base_url, api_key=api_key)


def embedding_model() -> str:
    return os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL)


def chunk_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    """Split text into chunks under max_chars, preferring sentence boundaries.

    Single short texts (the common case) return a 1-element list unchanged.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    sentences = _SENTENCE_SPLIT_RE.split(text)
    chunks: list[str] = []
    current = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) > max_chars:
            # Single sentence itself too long: hard-split on word boundaries.
            if current:
                chunks.append(current)
                current = ""
            words = sent.split(" ")
            piece = ""
            for w in words:
                candidate = (piece + " " + w).strip()
                if len(candidate) > max_chars and piece:
                    chunks.append(piece)
                    piece = w
                else:
                    piece = candidate
            if piece:
                chunks.append(piece)
            continue
        candidate = (current + " " + sent).strip() if current else sent
        if len(candidate) > max_chars:
            chunks.append(current)
            current = sent
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed a list of texts. Caller is responsible for chunking first."""
    if not texts:
        return []
    resp = _client().embeddings.create(input=texts, model=embedding_model())
    # Preserve input order (API returns results in request order via .index).
    ordered = sorted(resp.data, key=lambda d: d.index)
    return [d.embedding for d in ordered]


def embed_one_truncated(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> tuple[list[float], bool]:
    """Embed a single text (e.g. a search query), truncating if needed.

    Returns (embedding, was_truncated).
    """
    text = (text or "").strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return embed_texts([text])[0], truncated


def new_chunk_group_id() -> str:
    return str(uuid.uuid4())
