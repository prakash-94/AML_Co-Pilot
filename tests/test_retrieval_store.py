"""Vector store tests - a fake, deterministic embedder; no model download, no network.

The fake embeds each text as a small vector of keyword counts. It's not a real embedding,
but it's enough to prove the mechanical plumbing works: documents about "structuring" land
near a query about "structuring", and documents about "hawala" don't.
"""

from aml_copilot.retrieval import ChunkConfig
from aml_copilot.retrieval.store import build_index, search

KEYWORDS = ["structuring", "layering", "hawala", "shell", "threshold"]


def fake_embed(texts: list[str]) -> list[list[float]]:
    vectors = []
    for t in texts:
        lower = t.lower()
        vec = [float(lower.count(k)) for k in KEYWORDS]
        length = sum(v * v for v in vec) ** 0.5
        vectors.append([v / length for v in vec] if length else [0.0] * len(KEYWORDS))
    return vectors


def write_corpus(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "structuring.md").write_text(
        "# Structuring\n## Definition\n"
        "Structuring breaks deposits under the threshold, threshold, threshold.\n",
        encoding="utf-8",
    )
    (tmp_path / "hawala.md").write_text(
        "# Hawala\n## Definition\nHawala hawala hawala moves value through agents.\n",
        encoding="utf-8",
    )
    return tmp_path


# ----------------------------------------------------------------- build_index
def test_build_index_returns_the_chunk_count(tmp_path):
    source = write_corpus(tmp_path / "docs")
    persist = tmp_path / "chroma"
    count = build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)
    assert count == 2  # one Definition section per doc, each short enough to stay one chunk


def test_build_index_raises_on_an_empty_source_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    try:
        build_index(source_dir=empty, persist_dir=tmp_path / "chroma", embed_fn=fake_embed)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_rebuilding_the_index_does_not_duplicate_chunks(tmp_path):
    source = write_corpus(tmp_path / "docs")
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)
    count = build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)
    assert count == 2  # rebuilding twice still leaves exactly 2, not 4


# ---------------------------------------------------------------------- search
def test_search_ranks_the_matching_document_first(tmp_path):
    source = write_corpus(tmp_path / "docs")
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)

    results = search("threshold threshold", persist_dir=persist, embed_fn=fake_embed, top_k=2)
    assert results[0].chunk.source == "structuring.md"
    assert results[0].distance < results[1].distance


def test_search_result_carries_heading_and_source(tmp_path):
    source = write_corpus(tmp_path / "docs")
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)

    [top] = search("hawala", persist_dir=persist, embed_fn=fake_embed, top_k=1)
    assert top.chunk.source == "hawala.md"
    assert top.chunk.heading == "Definition"
    assert top.chunk.page is None


def test_search_respects_top_k(tmp_path):
    source = write_corpus(tmp_path / "docs")
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)
    assert len(search("structuring", persist_dir=persist, embed_fn=fake_embed, top_k=1)) == 1


def test_build_index_respects_a_custom_chunk_config(tmp_path):
    source = tmp_path / "docs"
    source.mkdir()
    long_body = " ".join(f"word{i}" for i in range(50))
    (source / "long.md").write_text(f"# Long\n## Section\n{long_body}\n", encoding="utf-8")
    persist = tmp_path / "chroma"

    count = build_index(
        source_dir=source, persist_dir=persist, embed_fn=fake_embed, chunk_config=ChunkConfig(max_words=20, overlap_words=5)
    )
    assert count > 1  # the 50-word section had to split under a 20-word window


# ------------------------------------------------- document types and skipping
def write_mixed_corpus(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "structuring.md").write_text(
        "# Structuring\n## Definition\nStructuring splits deposits under the threshold.\n", encoding="utf-8"
    )
    (root / "sar_narrative_standards.md").write_text(
        "# SAR Narrative Standards\n## Format\nA narrative states the threshold and the structuring facts.\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# Typology documents\n## Index\nThis folder covers structuring and threshold typologies.\n",
        encoding="utf-8",
    )
    return root


def test_readme_is_never_indexed(tmp_path):
    source = write_mixed_corpus(tmp_path / "docs")
    persist = tmp_path / "chroma"
    count = build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)
    assert count == 2  # structuring.md + sar_narrative_standards.md; README skipped

    sources = {r.chunk.source for r in search("structuring threshold", persist_dir=persist, embed_fn=fake_embed, top_k=10)}
    assert "README.md" not in sources


def test_chunks_are_tagged_with_their_document_type(tmp_path):
    source = write_mixed_corpus(tmp_path / "docs")
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)

    by_source = {r.chunk.source: r.chunk.doc_type for r in search("structuring", persist_dir=persist, embed_fn=fake_embed, top_k=10)}
    assert by_source == {"structuring.md": "typology", "sar_narrative_standards.md": "standard"}


def test_search_can_filter_to_typologies_only(tmp_path):
    source = write_mixed_corpus(tmp_path / "docs")
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)

    results = search("structuring threshold", persist_dir=persist, embed_fn=fake_embed, top_k=10, doc_type="typology")
    assert {r.chunk.source for r in results} == {"structuring.md"}


def test_search_without_an_index_raises_a_clear_error(tmp_path):
    from aml_copilot.errors import IndexNotBuiltError

    try:
        search("anything", persist_dir=tmp_path / "never_built", embed_fn=fake_embed)
        raise AssertionError("expected IndexNotBuiltError")
    except IndexNotBuiltError as exc:
        assert "retrieval build" in str(exc)


# ------------------------------------------------ project notes and source lists are "supporting"
def write_doc_with_supporting_sections(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "structuring.md").write_text(
        "# Structuring\n"
        "## Red flags\nDeposits just under the threshold, threshold, threshold.\n"
        "## How this project's detector maps to it\nOur detector flagged account ACC-S-00000 for structuring threshold.\n"
        "## Sources\n- A link about structuring and the threshold\n",
        encoding="utf-8",
    )
    return root


def test_project_notes_and_source_lists_are_tagged_supporting(tmp_path):
    source = write_doc_with_supporting_sections(tmp_path / "docs")
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)

    kinds = {r.chunk.heading: r.chunk.doc_type for r in search("structuring threshold", persist_dir=persist, embed_fn=fake_embed, top_k=10)}
    assert kinds == {
        "Red flags": "typology",
        "How this project's detector maps to it": "supporting",
        "Sources": "supporting",
    }


def test_typology_search_never_returns_supporting_sections(tmp_path):
    source = write_doc_with_supporting_sections(tmp_path / "docs")
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)

    results = search("structuring threshold", persist_dir=persist, embed_fn=fake_embed, top_k=10, doc_type="typology")
    assert [r.chunk.heading for r in results] == ["Red flags"]


def test_a_typology_files_header_block_before_its_first_heading_is_supporting(tmp_path):
    source = tmp_path / "docs"
    source.mkdir()
    (source / "structuring.md").write_text(
        "# Structuring\n\n**Stage:** Placement\n**Detector:** Not yet built\n\n## Red flags\nDeposits under the threshold.\n",
        encoding="utf-8",
    )
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)

    kinds = {r.chunk.heading: r.chunk.doc_type for r in search("structuring threshold", persist_dir=persist, embed_fn=fake_embed, top_k=10)}
    assert kinds == {None: "supporting", "Red flags": "typology"}


# ------------------------------------------------ investigation questions are "investigation"
def write_doc_with_questions(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "structuring.md").write_text(
        "# Structuring\n"
        "## Red flags\nDeposits just under the threshold, threshold, threshold.\n"
        "## Investigation questions\n- Where did the money go after the threshold deposits?\n",
        encoding="utf-8",
    )
    return root


def test_investigation_questions_get_their_own_tag(tmp_path):
    source = write_doc_with_questions(tmp_path / "docs")
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)

    kinds = {r.chunk.heading: r.chunk.doc_type for r in search("structuring threshold", persist_dir=persist, embed_fn=fake_embed, top_k=10)}
    assert kinds == {"Red flags": "typology", "Investigation questions": "investigation"}


def test_the_typology_judges_search_never_returns_investigation_questions(tmp_path):
    source = write_doc_with_questions(tmp_path / "docs")
    persist = tmp_path / "chroma"
    build_index(source_dir=source, persist_dir=persist, embed_fn=fake_embed)

    typology = search("threshold", persist_dir=persist, embed_fn=fake_embed, top_k=10, doc_type="typology")
    questions = search("threshold", persist_dir=persist, embed_fn=fake_embed, top_k=10, doc_type="investigation")
    assert [r.chunk.heading for r in typology] == ["Red flags"]
    assert [r.chunk.heading for r in questions] == ["Investigation questions"]
    assert questions[0].chunk.source == "structuring.md"  # tied to its typology file
