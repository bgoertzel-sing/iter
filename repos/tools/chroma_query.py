import importlib.util
from pathlib import Path
import chromadb

DESCRIPTION = "Query the PeTTa chroma_db for similar memories by text. Returns matching entries with their content and metadata. If a match is one chunk of a short multi-chunk note, the full note is reassembled; for a large chunked document, a bounded window around the match is shown instead."

# A note split into a handful of chunks (the common case: one paragraph that
# ran slightly long) is small enough to reassemble in full. A note split into
# dozens/hundreds of chunks (e.g. a whole ingested document) must NOT be
# reassembled in full on every match -- that reintroduces the exact
# over-length-context problem chunking exists to avoid. Above this threshold,
# only a bounded window around the matched chunk is shown.
MAX_FULL_REASSEMBLE_CHUNKS = 8
WINDOW_RADIUS = 2  # chunks shown on each side of the match when windowing


def _emb():
    p = Path(__file__).resolve().parent / "_singularity_embeddings.py"
    spec = importlib.util.spec_from_file_location("_singularity_embeddings", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sibling_chunks(collection, group):
    res = collection.get(where={"chunk_group": group}, include=["documents", "metadatas"])
    pairs = list(zip(res.get("ids", []), res.get("documents", []), res.get("metadatas", [])))
    pairs.sort(key=lambda p: (p[2] or {}).get("chunk_index", 0))
    return pairs

def _reassemble_or_window(collection, meta):
    """Small chunk group: reassemble in full. Large chunk group: return only a
    bounded window of chunks around the matched one, to avoid dumping an
    entire large document back into a single query result."""
    group = meta.get("chunk_group") if meta else None
    if not group:
        return None, False
    pairs = _sibling_chunks(collection, group)
    if not pairs:
        return None, False
    if len(pairs) <= MAX_FULL_REASSEMBLE_CHUNKS:
        return " ".join(doc for _id, doc, _m in pairs if doc), False
    idx = meta.get("chunk_index", 0)
    lo, hi = max(0, idx - WINDOW_RADIUS), min(len(pairs), idx + WINDOW_RADIUS + 1)
    window = " ".join(doc for _id, doc, _m in pairs[lo:hi] if doc)
    return window, True


def run(query, k=5):
    emb = _emb()
    query_embedding, truncated = emb.embed_one_truncated(query)

    db_path = str(Path.home() / "PeTTa" / "chroma_db")
    chroma_client = chromadb.PersistentClient(path=db_path)
    collection = chroma_client.get_or_create_collection(
        name="memories",
        embedding_function=None
    )

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=int(k),
        include=["documents", "metadatas", "distances"]
    )

    ids = results.get("ids", [[]])[0]
    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    if not documents:
        return "No results found."

    header = f"Found {len(documents)} results for query: \"{query}\"\n"
    if truncated:
        header += "(note: query text exceeded the embedding model's safe input size and was truncated before search)\n"
    output_lines = [header]
    for i, (id_val, doc, meta, dist) in enumerate(zip(ids, documents, metadatas, distances)):
        time_val = meta.get("time", "unknown") if meta else "unknown"
        linked = meta.get("linkedEpisodes", []) if meta else []
        strength = meta.get("strength", 1.0) if meta else 1.0
        confidence = meta.get("confidence", 0.5) if meta else 0.5
        formalization = meta.get("formalization", None) if meta else None
        output_lines.append(f"--- Result {i+1} (distance: {dist:.4f}) ---")
        output_lines.append(f"ID: {id_val}")
        output_lines.append(f"Time: {time_val}")
        output_lines.append(f"STV: ({strength}, {confidence})")
        if linked:
            output_lines.append(f"Linked Episodes: {linked}")
        if formalization:
            output_lines.append(f"Formalization: {formalization}")
        source = meta.get("source") if meta else None
        chunk_count = meta.get("chunk_count") if meta else None
        if source:
            output_lines.append(f"Source: {source}")
        if chunk_count:
            reassembled, windowed = _reassemble_or_window(collection, meta)
            output_lines.append(
                f"Content (chunk {meta.get('chunk_index', '?')+1}/{chunk_count} of group {meta.get('chunk_group')}): {doc}"
            )
            if reassembled and windowed:
                output_lines.append(
                    f"Nearby context (chunks {max(0, meta.get('chunk_index', 0)-WINDOW_RADIUS)+1}-"
                    f"{min(chunk_count, meta.get('chunk_index', 0)+WINDOW_RADIUS+1)} of {chunk_count}; "
                    f"full document not reassembled, too large): {reassembled}"
                )
            elif reassembled:
                output_lines.append(f"Full note (reassembled from {chunk_count} chunks): {reassembled}")
        else:
            output_lines.append(f"Content: {doc}")
        output_lines.append("")

    return "\n".join(output_lines)
