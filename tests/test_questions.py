"""Loading investigation questions straight from the typology markdown files."""

from aml_copilot.retrieval.questions import (
    DEFAULT_SOURCE_DIR,
    load_investigation_questions,
    questions_for_sources,
)

DOC = """# Structuring

**Stage:** Placement

## Red flags
- A red flag, not a question

## Investigation questions

- Where did the money go?
- Were the deposits made by others - and are those people linked to each other?
- A question that wraps
  onto a second line?

## Sources
- A link
"""


def write(root, name, text):
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text, encoding="utf-8")
    return root


def test_questions_are_read_word_for_word_including_dashes_and_wrapped_lines(tmp_path):
    root = write(tmp_path, "structuring.md", DOC)
    assert load_investigation_questions("structuring.md", root) == [
        "Where did the money go?",
        "Were the deposits made by others - and are those people linked to each other?",
        "A question that wraps onto a second line?",
    ]


def test_only_the_questions_section_is_read(tmp_path):
    root = write(tmp_path, "structuring.md", DOC)
    questions = load_investigation_questions("structuring.md", root)
    assert "A red flag, not a question" not in questions and "A link" not in questions


def test_a_document_without_the_section_has_no_questions(tmp_path):
    root = write(tmp_path, "plain.md", "# Plain\n\n## Red flags\n- one\n")
    assert load_investigation_questions("plain.md", root) == []


def test_missing_files_other_formats_and_paths_give_no_questions(tmp_path):
    root = write(tmp_path, "structuring.md", DOC)
    (root / "report.pdf").write_bytes(b"%PDF")
    assert load_investigation_questions("nope.md", root) == []
    assert load_investigation_questions("report.pdf", root) == []
    assert load_investigation_questions("../structuring.md", root) == []  # never a path


def test_a_match_on_the_hawala_pdf_uses_the_questions_of_its_markdown_summary(tmp_path):
    root = write(tmp_path, "hawala_informal_value_transfer.md", DOC)
    assert load_investigation_questions("fatf_hawala_hossp_2013.pdf", root)[0] == "Where did the money go?"


def test_questions_for_several_documents_are_merged_without_duplicates(tmp_path):
    root = write(tmp_path, "a.md", DOC)
    write(root, "b.md", DOC.replace("Where did the money go?", "A different question?"))
    merged = questions_for_sources(["a.md", "b.md"], root)
    assert merged[0] == "Where did the money go?" and "A different question?" in merged
    assert len(merged) == len(set(merged)) == 4


def test_every_real_typology_document_has_investigation_questions():
    for path in sorted(DEFAULT_SOURCE_DIR.glob("*.md")):
        if path.name in ("README.md", "sar_narrative_standards.md"):
            continue
        questions = load_investigation_questions(path.name)
        assert len(questions) >= 5, path.name
        assert all(q.endswith("?") for q in questions), path.name
