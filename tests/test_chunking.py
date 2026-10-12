"""Chunking tests - pure text logic, no embeddings, no network."""

from pathlib import Path

from aml_copilot.retrieval import (
    Chunk,
    ChunkConfig,
    chunk_document,
    chunk_markdown,
    chunk_pdf,
    chunk_text,
)
from aml_copilot.retrieval.chunking import _split_markdown_sections

TYPOLOGY_DOCS = Path(__file__).resolve().parents[1] / "typology_docs"


# --------------------------------------------------------------- chunk_text
def test_short_text_stays_as_one_chunk():
    assert chunk_text("just a few words here") == ["just a few words here"]


def test_empty_text_produces_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_long_text_splits_with_overlap():
    words = [f"w{i}" for i in range(100)]
    text = " ".join(words)
    cfg = ChunkConfig(max_words=30, overlap_words=10)
    chunks = chunk_text(text, cfg)

    assert len(chunks) > 1
    first_words = chunks[0].split()
    second_words = chunks[1].split()
    assert len(first_words) == 30
    # the last 10 words of chunk 1 are the first 10 of chunk 2 - the overlap
    assert first_words[-10:] == second_words[:10]


def test_chunks_cover_every_word_with_no_gaps():
    words = [f"w{i}" for i in range(97)]  # not an exact multiple of the window
    cfg = ChunkConfig(max_words=25, overlap_words=5)
    chunks = chunk_text(" ".join(words), cfg)
    assert chunks[-1].split()[-1] == "w96"  # the final word is never dropped


# --------------------------------------------------------- markdown sections
def test_split_markdown_sections_separates_on_h2_headings():
    text = "# Title\nintro line\n## First\nbody one\n## Second\nbody two\n"
    sections = _split_markdown_sections(text)
    assert sections == [(None, "# Title\nintro line"), ("First", "body one"), ("Second", "body two")]


def test_split_markdown_sections_drops_empty_sections():
    text = "## Empty\n\n## Real\nsome content\n"
    sections = _split_markdown_sections(text)
    assert sections == [("Real", "some content")]


def test_chunk_markdown_prefixes_title_and_heading():
    text = "# Structuring\n## Red flags\nDeposits just under the threshold.\n"
    chunks = chunk_markdown(text, "structuring.md")
    assert len(chunks) == 1
    assert chunks[0].text == "Structuring - Red flags: Deposits just under the threshold."
    assert chunks[0].source == "structuring.md"
    assert chunks[0].heading == "Red flags"
    assert chunks[0].id == "structuring.md::Red flags::0"


def test_chunk_markdown_ids_are_unique_across_sections():
    text = "# T\n## A\nbody a\n## B\nbody b\n"
    ids = [c.id for c in chunk_markdown(text, "doc.md")]
    assert len(ids) == len(set(ids))


def test_chunk_document_dispatches_on_extension(tmp_path):
    md = tmp_path / "x.md"
    md.write_text("# T\n## H\nbody\n", encoding="utf-8")
    chunks = chunk_document(md)
    assert chunks and chunks[0].source == "x.md"


def test_chunk_document_rejects_unsupported_extension(tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("hello")
    try:
        chunk_document(bad)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# ------------------------------------------------------- real documents
def test_chunk_markdown_on_every_real_typology_doc():
    md_files = sorted(TYPOLOGY_DOCS.glob("*.md"))
    assert len(md_files) >= 8  # 7 typologies + SAR standards (README excluded below)
    for path in md_files:
        if path.name == "README.md":
            continue
        chunks = chunk_markdown(path.read_text(encoding="utf-8"), path.name)
        assert chunks, f"{path.name} produced no chunks"
        assert all(c.source == path.name for c in chunks)
        assert all(c.text.strip() for c in chunks)


def test_chunk_pdf_on_the_real_fatf_report():
    pdf = TYPOLOGY_DOCS / "fatf_hawala_hossp_2013.pdf"
    chunks = chunk_pdf(pdf)
    assert len(chunks) > 20  # a 70-page report should produce well more than 20 chunks
    assert all(isinstance(c, Chunk) and c.page is not None for c in chunks)
    assert all(c.source == "fatf_hawala_hossp_2013.pdf" for c in chunks)
    # the real red-flags section we read manually should land in some chunk
    assert any("collective accounts" in c.text.lower() for c in chunks)
