"""F34: cached sentence-embedding index for hybrid (keyword + vector) recall.

Model: BAAI/bge-small-en-v1.5 (384-dim, CPU). Embeddings are cached by
sha1(text) in an .npz so each journal entry is encoded once. Everything
degrades gracefully: if sentence-transformers or the model is unavailable,
available() is False and callers fall back to keyword-only recall.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

MODEL_NAME = os.environ.get("WMTM_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
_DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "memory" / "_wmtm_embed_cache.npz"

_model = None
_model_failed = False


def _get_model():
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
    except Exception:
        _model_failed = True
        _model = None
    return _model


def available() -> bool:
    return _get_model() is not None


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


class EmbedIndex:
    def __init__(self, cache_path=None):
        import numpy as np
        self._np = np
        self.cache_path = Path(cache_path or os.environ.get("WMTM_EMBED_CACHE", _DEFAULT_CACHE))
        self._vecs: dict[str, "np.ndarray"] = {}
        self._dirty = False
        if self.cache_path.exists():
            try:
                d = np.load(self.cache_path)
                for k, v in zip(d["keys"], d["vecs"]):
                    self._vecs[str(k)] = v
            except Exception:
                self._vecs = {}

    def save(self):
        if not self._dirty:
            return
        np = self._np
        keys = list(self._vecs)
        tmp = self.cache_path.with_suffix(".tmp.npz")
        np.savez(tmp, keys=np.array(keys), vecs=np.stack([self._vecs[k] for k in keys]) if keys else np.zeros((0, 384), dtype="float32"))
        os.replace(tmp, self.cache_path)
        self._dirty = False

    def ensure(self, texts, batch_size=64):
        missing = {}
        for t in texts:
            k = _h(t)
            if k not in self._vecs:
                missing[k] = t
        if missing:
            m = _get_model()
            if m is None:
                return False
            ks = list(missing)
            vs = m.encode([missing[k][:2000] for k in ks], batch_size=batch_size,
                          normalize_embeddings=True, show_progress_bar=False)
            for k, v in zip(ks, vs):
                self._vecs[k] = v.astype("float32")
            self._dirty = True
        return True

    def rank(self, query: str, clusters, k=50):
        """Return [(cluster_id, cosine)] for clusters, best first."""
        np = self._np
        m = _get_model()
        if m is None or not clusters:
            return []
        texts = [c.event_note or c.text for c in clusters]
        if not self.ensure(texts):
            return []
        mat = np.stack([self._vecs[_h(t)] for t in texts])
        q = m.encode([QUERY_PREFIX + query], normalize_embeddings=True, show_progress_bar=False)[0]
        sims = mat @ q
        order = np.argsort(-sims)[:k]
        return [(clusters[i].id, float(sims[i])) for i in order]


def rrf(*ranked_lists, k=60, top=15):
    """Reciprocal rank fusion over lists of ids."""
    score: dict[str, float] = {}
    for lst in ranked_lists:
        for r, cid in enumerate(lst):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + r + 1)
    return [c for c, _ in sorted(score.items(), key=lambda x: -x[1])[:top]]
