# SAR Narrative Standards

**Type:** Writing/format standard, not a typology
**Consumer:** the Narrative Agent (`aml_copilot/agents/narrative.py`)
**Primary source:** FinCEN's own SAR Narrative Guidance package (2003), still the standing
reference for narrative quality; supplementary legal threshold from the SEC's 2008 AML Source
Tool (see note at the bottom on why that document wasn't used for the typology corpus).

## When a SAR applies at all

A SAR is required when a transaction is conducted through the institution, involves at least
$5,000, and the institution knows, suspects, or has reason to suspect it involves funds from
illegal activity, is structured to evade BSA requirements, has no legitimate business purpose
the institution can identify, or facilitates criminal activity. This project's own definitions
file (`aml_copilot/errors.py` docstrings, project doc Section 3.4) already distinguishes SARs
from CTRs on exactly this basis: a CTR is mechanical (any cash transaction over $10,000); a
SAR requires judgment, which is why this project keeps a human approval gate before anything
is considered filed.

## What makes a narrative acceptable

FinCEN's own guidance sets a specific bar: a complete narrative answers five questions —
**who, what, when, where, why** — plus a sixth, **how** (the modus operandi), and presents
them in chronological order with specific dates and individual amounts, not aggregated
totals. The guidance recommends three parts:

1. **Introduction** — why this SAR is being filed, in one or two sentences
2. **Body** — the 5 Ws and How, in chronological order, each claim tied to a specific fact
3. **Conclusion** — a summary, plus any follow-up action taken or still pending

## How this project's own data already maps to each element

This is the part worth building against directly. The Evidence Agent's output
(`EvidenceSummary`) and the Detection rule engine's output (`RiskScore`) already carry
almost everything a narrative needs — the Narrative Agent's job is closer to
*re-ordering and re-wording already-cited facts into this format* than to inventing new
analysis:

| FinCEN element | Where it already lives in this project |
|---|---|
| **Who** | `EvidenceSummary.profile_summary` — account holder, occupation, risk rating, PEP status |
| **What** | `EvidenceSummary.transaction_summary` + `network_summary` — specific transactions and amounts, not aggregates, matching FinCEN's own preference |
| **When / Where** | Already embedded in each transaction's `timestamp`, `branch_id`, and `country` fields, carried through into the prompt and citations |
| **Why** | `EvidenceSummary.detection_summary` — the rule engine's score and reasons, and eventually the matched typology from the Typology/RAG Agent |
| **How** | `network_summary`'s hop-by-hop chain description (the modus operandi: a 3-hop transfer completing in 17 hours with shrinking amounts, for example) |
| **Every claim cited** | `EvidenceSummary.citations` already requires a transaction or case id per claim — this is the same discipline FinCEN's "specific dates and amounts, not aggregates" rule is asking for, already built in one stage earlier than the Narrative Agent |

## What the Narrative Agent adds on top of the evidence

- It renders the above into FinCEN's three-part structure - introduction, facts (the body) and
  conclusion - as a list of sentences, each carrying the transaction or case ids it rests on,
  rather than the five separate labeled fields `EvidenceSummary` uses
- It states the matched typology by name, taken from the Typology/RAG Agent's validated matches
  (`detection_summary` alone reports a numeric score and reasons, not a named pattern)
- It stays within the same boundary every earlier agent in this project holds: it drafts an
  *explanation* of why the evidence fits a typology, but never decides that the SAR should be
  filed — that stays the human investigator's call at the approval gate
- It attaches the matched typologies' investigation questions word for word; the model never
  writes them

## How the draft is checked in code

The model's draft is never trusted as-is (`finalize_narrative` in `narrative.py`):

- A sentence containing a dollar amount, a date or an id that appears nowhere in the evidence is
  dropped, and the drop is reported in `warnings`
- Evidence ids that do not exist are removed; an amount with no id, verdict-like wording
  ("suspicious", "should file") and a transaction the rule engine flagged that no sentence cites
  each raise a warning
- The status (`draft_for_human_review`) is set by code, never by the model

This checks figures, dates and ids - not whether a sentence's *meaning* is faithful to the
evidence; that deeper check belongs to the later guardrails phase.

## Why the SEC document wasn't used for this

The SEC's 2008 AML Source Tool is a legal citation index for broker-dealers, not narrative
guidance — its only directly relevant content was the legal $5,000/suspicion threshold quoted
above. The actual structure and quality standard for the narrative itself comes from FinCEN's
own guidance package, which is the right source for that question regardless of industry.

## Sources

- [FinCEN — SAR Narrative Guidance package](https://www.fincen.gov/resources/statutes-regulations/guidance/sar-narrative-guidance-package)
- [FinCEN — SAR Activity Review / Narrative completion guidance (2003)](https://www.fincen.gov/system/files/shared/sarnarrcompletguidfinal_112003.pdf)
- SEC, *Anti-Money Laundering (AML) Source Tool for Broker-Dealers* (2008) — Section 7, for the legal filing threshold only
