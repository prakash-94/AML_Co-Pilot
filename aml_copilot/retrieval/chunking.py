"""Split typology documents into embeddable chunks.

Markdown files are split on `## ` headings first - keeping each section (Definition, Red
flags, ...) together as one semantic unit - then any section still too long is further split
into overlapping word-windows. The PDF has no heading structure worth exploiting, so it's
chunked per page instead, with any long page further word-windowed. The overlap exists so a
fact sitting right at a chunk boundary still appears whole in at least one chunk.
"""

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True)
class ChunkConfig:
    max_words: int = 220
    overlap_words: int = 40


@dataclass
class Chunk:
    id: str
    text: str
    source: str
    heading: str | None = None
    page: int | None = None
    doc_type: str = "typology"  # "typology" or "standard"; set by the store, not the chunker


def _window(words: list[str], max_words: int, overlap_words: int) -> list[list[str]]:
    if len(words) <= max_words:
        return [words]
    step = max_words - overlap_words
    windows = []
    start = 0
    while True:
        windows.append(words[start : start + max_words])
        if start + max_words >= len(words):
            break
        start += step
    return windows


def chunk_text(text: str, config: ChunkConfig | None = None) -> list[str]:
    cfg = config or ChunkConfig()
    words = text.split()
    if not words:
        return []
    return [" ".join(w) for w in _window(words, cfg.max_words, cfg.overlap_words)]


def _split_markdown_sections(text: str) -> list[tuple[str | None, str]]:
    """(heading, body) pairs, split on '## ' headings. Anything before the first one
    (the title, any intro line) is returned under heading None."""
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(body).strip()) for h, body in sections if "\n".join(body).strip()]


def chunk_markdown(text: str, source: str, config: ChunkConfig | None = None) -> list[Chunk]:
    cfg = config or ChunkConfig()
    title_line = next((line for line in text.splitlines() if line.startswith("# ")), "")
    title = title_line[2:].strip()

    chunks = []
    for heading, body in _split_markdown_sections(text):
        if heading is None and title_line:
            # The intro section's body still contains the raw "# Title" line (the splitter
            # doesn't special-case it). Strip it out here - it's already captured in `title`
            # and prefixed onto every chunk below, so leaving it in would duplicate it.
            body = "\n".join(line for line in body.splitlines() if line != title_line).strip()
            if not body:
                continue
        prefix = f"{title} - {heading}: " if heading else f"{title}: "
        for i, piece in enumerate(chunk_text(body, cfg)):
            chunks.append(
                Chunk(id=f"{source}::{heading or 'intro'}::{i}", text=prefix + piece, source=source, heading=heading)
            )
    return chunks


def chunk_pdf(path: Path, config: ChunkConfig | None = None) -> list[Chunk]:
    cfg = config or ChunkConfig()
    reader = PdfReader(str(path))

    chunks = []
    for page_num, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        for i, piece in enumerate(chunk_text(text, cfg)):
            chunks.append(Chunk(id=f"{path.name}::p{page_num}::{i}", text=piece, source=path.name, page=page_num))
    return chunks


def chunk_document(path: Path, config: ChunkConfig | None = None) -> list[Chunk]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return chunk_pdf(path, config)
    if suffix in (".md", ".markdown"):
        return chunk_markdown(path.read_text(encoding="utf-8"), path.name, config)
    raise ValueError(f"Unsupported document type: {path.suffix}")
