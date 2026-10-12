# Typology documents

This folder is the content corpus for the project's RAG layer (Phase 2 "Vector store" →
Phase 4 "Typology/RAG Agent"). Each file covers one laundering typology: what it is, how it
typically works, the red flags examiners and regulators publish, and — where one exists — how
this project's own detection code (`aml_copilot/detection/`) maps to it. Six typologies come
from the project's own context doc (Section 3.3); Hawala was added afterward from a real FATF
report the user supplied, and is genuinely useful RAG material even though it wasn't in the
original scope.

**Status of detector coverage:**

| Typology | Stage | Detector built? |
|---|---|---|
| Structuring | Placement | Yes — `structuring.py` |
| Cash-intensive business | Placement | No |
| Rapid wire transfers / layering | Layering | Yes — `layering.py` |
| Shell companies | Layering | No |
| Crypto conversion cycling | Layering | No |
| Real estate / luxury asset integration | Integration | No |
| Hawala / informal value transfer | Layering | No |

**Primary source documents** (not summaries — kept as-is for the vector store to chunk
directly): `fatf_hawala_hossp_2013.pdf` — FATF, *The Role of Hawala and Other Similar Service
Providers in Money Laundering and Terrorist Financing* (October 2013).

**Layout of each typology file:** a header (`Typology ID` TYP-001 to TYP-007, `Stage`,
`Channels`, `Detector`), then Definition, How it works, Red flags, **Investigation questions**,
How this project's detector maps to it, and Sources. The investigation questions are what an
analyst should verify next (source of funds, links between parties, documented explanations);
they are written from each file's own red flags and make no new factual claims. There is
deliberately no per-typology "risk level" field: no public source rates a typology's risk, so
any value would be an invented number presented as fact.

**How the index uses these files** (`aml_copilot/retrieval/store.py`): `README.md` is never
indexed. Each chunk is tagged with a `doc_type` — `typology` for the knowledge itself,
`standard` for `sar_narrative_standards.md`, `investigation` for each file's "Investigation
questions" section, and `supporting` for the header block and the "How this project's detector
maps to it" and "Sources" sections. The Typology/RAG Agent searches `typology` only: the
project-notes sections quote this project's own accounts back at the matcher (circular
evidence), the source lists carry no knowledge, and the questions are guidance rather than
evidence of a match. The Narrative Agent (`aml_copilot/agents/narrative.py`) does not search
the `investigation` chunks - the index stores them as flattened prose cut up for embedding - it
reads each matched typology file's exact bullet list through
`aml_copilot/retrieval/questions.py` and attaches the questions word for word.

**`sar_narrative_standards.md` is not a typology** — it's the writing-format standard the
Narrative Agent follows: what FinCEN's own guidance requires a SAR narrative to contain, and a
table mapping each requirement to where the data already lives in this project's
`EvidenceSummary`/`RiskScore` output.

**On sourcing:** this content is written from publicly available FinCEN, FFIEC, and FATF
guidance (linked at the bottom of each file), paraphrased and synthesized for this project —
it is not a verbatim reproduction of any single document, and it is **not legal or
compliance advice**. Verify against the primary sources before relying on this for anything
beyond this portfolio project; some source advisories are several years old and may have
been updated since.
