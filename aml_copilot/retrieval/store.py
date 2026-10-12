"""Embed chunked typology documents into a local ChromaDB collection, and search it.

Embeddings run locally via sentence-transformers - no API key, no per-call cost, unlike the
Evidence Agent's Claude calls. The embedding function is injectable (`embed_fn`), the same
pattern used throughout this project for everything external, so tests can run without
downloading the real ~80MB model.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

import chromadb
from chromadb.errors import NotFoundError

from aml_copilot.errors import IndexNotBuiltError
from aml_copilot.retrieval.chunking import Chunk, ChunkConfig, chunk_document

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "typology_docs"
DEFAULT_PERSIST_DIR = PROJECT_ROOT / ".chroma"
COLLECTION_NAME = "typology_docs"

# The folder holds more than typologies. The README documents the folder itself and is never
# indexed; the SAR narrative standard is a writing guide for the Narrative Agent, so it is
# indexed but tagged "standard" - typology search filters it out. Every other file is a
# "typology" by default, so adding a new typology document needs no change here.
SKIP_FILES = frozenset({"README.md"})
DOC_TYPES = {"sar_narrative_standards.md": "standard"}

# Sections inside a typology document that are *about* the document, not the typology: the
# project's own detector notes - which quote this project's own accounts back at the matcher,
# so retrieving them is circular - and bare lists of source links. They stay in the index but
# are tagged "supporting", so a typology search (`doc_type="typology"`) never returns them.
SUPPORTING_HEADINGS = frozenset({"How this project's detector maps to it", "Sources"})
# A markdown typology file also opens with a header block ("Stage: ... / Detector: Not yet
# built") before its first heading. That is project metadata too, and it was showing up as if it
# were typology knowledge - so a chunk with neither a heading nor a PDF page is "supporting".

# "Investigation questions" tell an analyst (and the Narrative Agent) what to verify next. They
# are not evidence that an account resembles the typology, so the typology judge must not see
# them next to the red flags - they get their own "investigation" tag instead.
INVESTIGATION_HEADINGS = frozenset({"Investigation questions"})


def _is_supporting(chunk: Chunk) -> bool:
    return chunk.heading in SUPPORTING_HEADINGS or (chunk.heading is None and chunk.page is None)


def _tag_typology_chunk(chunk: Chunk) -> str:
    """The `doc_type` for one chunk of a typology file."""
    if chunk.heading in INVESTIGATION_HEADINGS:
        return "investigation"
    return "supporting" if _is_supporting(chunk) else "typology"


EmbedFn = Callable[[list[str]], list[list[float]]]


@dataclass
class RetrievedChunk:
    chunk: Chunk
    distance: float  # cosine distance - lower means more similar


@lru_cache(maxsize=2)
def default_embedder(model_name: str = "all-MiniLM-L6-v2") -> EmbedFn:
    """The real embedder: a small local sentence-transformers model. Normalizes vectors to
    unit length, so cosine distance in the store behaves as expected.

    Cached: loading the model takes seconds, so a process that investigates many accounts loads
    it once instead of once per account."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)

    def embed(texts: list[str]) -> list[list[float]]:
        return model.encode(texts, normalize_embeddings=True).tolist()

    return embed


def _collect_chunks(source_dir: Path, config: ChunkConfig | None = None) -> list[Chunk]:
    chunks = []
    for path in sorted(source_dir.iterdir()):
        if path.name in SKIP_FILES or path.suffix.lower() not in (".md", ".markdown", ".pdf"):
            continue
        doc_type = DOC_TYPES.get(path.name, "typology")
        for chunk in chunk_document(path, config):
            tag = _tag_typology_chunk(chunk) if doc_type == "typology" else doc_type
            chunks.append(replace(chunk, doc_type=tag))
    return chunks


def build_index(
    source_dir: Path = DEFAULT_SOURCE_DIR,
    persist_dir: Path = DEFAULT_PERSIST_DIR,
    embed_fn: EmbedFn | None = None,
    chunk_config: ChunkConfig | None = None,
) -> int:
    """(Re)build the collection from every document in `source_dir`. Returns the chunk count."""
    embed_fn = embed_fn or default_embedder()
    chunks = _collect_chunks(source_dir, chunk_config)
    if not chunks:
        raise ValueError(f"No documents found in {source_dir}")

    client = chromadb.PersistentClient(path=str(persist_dir))
    if COLLECTION_NAME in {c.name for c in client.list_collections()}:
        client.delete_collection(COLLECTION_NAME)
    collection = client.create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})

    embeddings = embed_fn([c.text for c in chunks])
    collection.add(
        ids=[c.id for c in chunks],
        documents=[c.text for c in chunks],
        embeddings=embeddings,
        metadatas=[
            {"source": c.source, "heading": c.heading or "", "page": c.page or 0, "doc_type": c.doc_type}
            for c in chunks
        ],
    )
    return len(chunks)


def search(
    query: str,
    top_k: int = 5,
    persist_dir: Path = DEFAULT_PERSIST_DIR,
    embed_fn: EmbedFn | None = None,
    doc_type: str | None = None,
) -> list[RetrievedChunk]:
    """Nearest chunks to `query`. Pass `doc_type="typology"` to search typologies only."""
    embed_fn = embed_fn or default_embedder()
    client = chromadb.PersistentClient(path=str(persist_dir))
    try:
        collection = client.get_collection(COLLECTION_NAME)
    except NotFoundError as exc:
        raise IndexNotBuiltError(
            "The vector index has not been built - run `python -m aml_copilot.retrieval build`"
        ) from exc

    [query_embedding] = embed_fn([query])
    where = {"doc_type": doc_type} if doc_type else None
    result = collection.query(query_embeddings=[query_embedding], n_results=top_k, where=where)

    out = []
    for doc, meta, dist, cid in zip(
        result["documents"][0], result["metadatas"][0], result["distances"][0], result["ids"][0], strict=True
    ):
        chunk = Chunk(
            id=cid,
            text=doc,
            source=meta["source"],
            heading=meta["heading"] or None,
            page=meta["page"] or None,
            doc_type=meta.get("doc_type", "typology"),
        )
        out.append(RetrievedChunk(chunk=chunk, distance=dist))
    return out
