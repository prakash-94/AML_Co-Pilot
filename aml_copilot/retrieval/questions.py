"""Load the "Investigation questions" of typology documents, word for word.

The vector index also holds these sections (tagged "investigation"), but as flattened prose cut
up for embedding - splitting that back into questions would corrupt any question containing a
dash. So the exact bullet list is read straight from the markdown file instead: no model, no
embeddings, same answer every time.
"""

from collections.abc import Iterable
from pathlib import Path

from aml_copilot.retrieval.store import DEFAULT_SOURCE_DIR

SECTION_HEADING = "## Investigation questions"

# A match can cite the primary-source PDF rather than the markdown summary written from it. The
# summary is where the questions live.
_COMPANION_DOCUMENT = {"fatf_hawala_hossp_2013.pdf": "hawala_informal_value_transfer.md"}


def load_investigation_questions(source_name: str, source_dir: Path = DEFAULT_SOURCE_DIR) -> list[str]:
    """The questions in one typology document, or [] if it has none (or is not a document here)."""
    source_name = _COMPANION_DOCUMENT.get(source_name, source_name)
    if Path(source_name).name != source_name:  # a bare filename only - never a path
        return []
    path = source_dir / source_name
    if path.suffix.lower() not in (".md", ".markdown") or not path.is_file():
        return []

    questions: list[str] = []
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == SECTION_HEADING:
            in_section = True
        elif in_section and line.startswith("## "):
            break  # the next section begins
        elif in_section and line.startswith("- "):
            questions.append(line[2:].strip())
        elif in_section and questions and line.startswith("  ") and line.strip():
            questions[-1] += " " + line.strip()  # a bullet wrapped onto a second line
    return questions


def questions_for_sources(sources: Iterable[str], source_dir: Path = DEFAULT_SOURCE_DIR) -> list[str]:
    """Questions for several documents, in order, with duplicates removed."""
    merged: list[str] = []
    for source in sources:
        merged.extend(load_investigation_questions(source, source_dir))
    return list(dict.fromkeys(merged))
