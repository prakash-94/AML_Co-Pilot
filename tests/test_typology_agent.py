"""Typology/RAG Agent tests - fake search and fake judge: no model download, no index, no API key."""

import json

import pytest

from aml_copilot.agents import typology
from aml_copilot.agents.models import EvidenceSummary, TypologyJudgment, TypologyMatch
from aml_copilot.agents.typology import (
    SYSTEM_PROMPT,
    TypologyConfig,
    build_queries,
    build_typology_graph,
    build_typology_prompt,
    finalize_assessment,
    match_typology,
    merge_passages,
)
from aml_copilot.config import Settings
from aml_copilot.detection import LayeringSignal, RiskScore, StructuringSignal
from aml_copilot.errors import ConfigError, IndexNotBuiltError
from aml_copilot.retrieval import Chunk
from aml_copilot.retrieval.store import RetrievedChunk

ACC = "ACC-L-00000"
LAYERING_DOC = "rapid_wire_transfers.md"
STRUCTURING_DOC = "structuring.md"
HAWALA_DOC = "hawala_informal_value_transfer.md"


# ------------------------------------------------------------------------ builders
def passage(source=LAYERING_DOC, heading="Red flags", distance=0.4, text="passage text", page=None, chunk_id=None):
    chunk = Chunk(id=chunk_id or f"{source}::{heading}::0", text=text, source=source, heading=heading, page=page)
    return RetrievedChunk(chunk=chunk, distance=distance)


def evidence(**kw):
    defaults = {
        "account_id": ACC,
        "profile_summary": "Teacher, High risk, PEP.",
        "transaction_summary": "One wire of $44,587.98.",
        "network_summary": "Three related accounts.",
        "case_history_summary": "CASEHISTORY-MARKER",
        "detection_summary": "Rule engine scored 50/100.",
        "citations": {"wire transfer": ["TXN1"], "3-hop chain": ["TXN1", "TXN2"]},
    }
    return EvidenceSummary(**{**defaults, **kw})


def risk(structuring=False, layering=False):
    return RiskScore(
        score=50 * (structuring + layering),
        tier="medium" if (structuring or layering) else "low",
        reasons=["Layering signal: 3-hop chain"] if layering else ["No structuring or layering signals detected"],
        structuring=StructuringSignal(flagged=structuring),
        layering=LayeringSignal(flagged=layering),
    )


def match(source=LAYERING_DOC, confidence="high", ids=("TXN1",), typology_name="Layering"):
    return TypologyMatch(
        typology=typology_name, source_document=source, confidence=confidence,
        matched_red_flags=["a red flag the evidence supports"],  # a match with none is dropped (see below)
        rationale="because", evidence_ids=list(ids),
    )


class FakeSearch:
    def __init__(self, results=None, error=None):
        self.results, self.error, self.calls = results if results is not None else [], error, []

    def __call__(self, query, top_k):
        self.calls.append((query, top_k))
        if self.error:
            raise self.error
        return self.results


class FakeJudge:
    def __init__(self, judgment=None):
        self.prompts = []
        self.judgment = judgment or TypologyJudgment(matches=[match()])

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.judgment


def run(ev=None, rk=None, search=None, judge=None, config=None):
    search = search if search is not None else FakeSearch([passage()])
    graph = build_typology_graph(search, judge or FakeJudge(), config)
    return graph.invoke({"evidence": ev or evidence(), "risk_score": rk or risk(layering=True)})


# --------------------------------------------------------------------- build_queries
def test_queries_are_the_three_pattern_fields_in_order():
    assert build_queries(evidence()) == ["Rule engine scored 50/100.", "Three related accounts.", "One wire of $44,587.98."]


def test_blank_fields_are_skipped_and_text_is_stripped():
    ev = evidence(detection_summary="  ", network_summary="  padded  ")
    assert build_queries(ev) == ["padded", "One wire of $44,587.98."]


def test_case_history_and_profile_are_not_queried():
    queries = " ".join(build_queries(evidence()))
    assert "CASEHISTORY-MARKER" not in queries and "Teacher" not in queries


# ------------------------------------------------------------------- merge_passages
def test_merge_keeps_each_passage_once_at_its_best_distance():
    results = [[passage(distance=0.6)], [passage(distance=0.3)]]
    [merged] = merge_passages(results, TypologyConfig())
    assert merged.distance == 0.3


def test_merge_drops_passages_beyond_max_distance():
    results = [[passage(chunk_id="near", distance=0.5), passage(chunk_id="far", distance=0.9)]]
    merged = merge_passages(results, TypologyConfig(max_distance=0.75))
    assert [m.chunk.id for m in merged] == ["near"]


def test_merge_sorts_closest_first_and_caps_the_count():
    results = [[passage(chunk_id=f"c{i}", distance=d) for i, d in enumerate([0.7, 0.2, 0.5, 0.3])]]
    merged = merge_passages(results, TypologyConfig(max_passages=3))
    assert [m.distance for m in merged] == [0.2, 0.3, 0.5]


# ----------------------------------------------------------------------- the prompt
def test_prompt_contains_evidence_rule_engine_citations_and_passages():
    prompt = build_typology_prompt(
        evidence(), risk(layering=True), [passage(text="Layering moves money fast.", heading="Red flags")]
    )
    assert ACC in prompt and "Teacher, High risk, PEP." in prompt
    assert "score 50/100 (medium)" in prompt and "Layering signal: 3-hop chain" in prompt
    assert "3-hop chain: TXN1, TXN2" in prompt
    assert f"source={LAYERING_DOC} section Red flags" in prompt and "Layering moves money fast." in prompt


def test_prompt_labels_pdf_passages_by_page():
    prompt = build_typology_prompt(evidence(), risk(), [passage(source="fatf.pdf", heading=None, page=57)])
    assert "source=fatf.pdf page 57" in prompt


def test_prompt_forbids_judging_guilt_and_leaves_case_history_out():
    prompt = build_typology_prompt(evidence(), risk(), [passage()])
    assert "never guilt" in prompt and "do not recommend filing" in prompt
    assert "CASEHISTORY-MARKER" not in prompt


# --------------------------------------------------------------- finalize_assessment
def finalize(judgment, passages=None, ev=None, rk=None):
    return finalize_assessment(ev or evidence(), rk or risk(layering=True), passages or [passage()], judgment)


def test_a_grounded_match_survives_with_its_ids():
    result = finalize(TypologyJudgment(matches=[match(ids=("TXN1", "TXN2"))]))
    [kept] = result.matches
    assert kept.source_document == LAYERING_DOC and kept.evidence_ids == ["TXN1", "TXN2"]
    assert result.no_match_reason == "" and result.warnings == []


def test_match_on_a_document_that_was_never_retrieved_is_dropped():
    result = finalize(TypologyJudgment(matches=[match(source="made_up.md")]))
    assert result.matches == []
    assert "was not among the retrieved passages" in result.warnings[0]
    assert "failed validation" in result.no_match_reason


def test_invented_evidence_ids_are_stripped_and_reported():
    result = finalize(TypologyJudgment(matches=[match(ids=("TXN1", "TXN999"))]))
    assert result.matches[0].evidence_ids == ["TXN1"]
    assert "TXN999" in result.warnings[0]


def test_a_match_citing_only_invented_ids_is_dropped():
    result = finalize(TypologyJudgment(matches=[match(ids=("TXN999",))]))
    assert result.matches == []
    assert any("cites no valid evidence id" in w for w in result.warnings)


def test_matches_are_ranked_high_then_medium_then_low_with_ties_stable():
    judgment = TypologyJudgment(matches=[
        match(source="a.md", confidence="low"),
        match(source="b.md", confidence="high"),
        match(source="c.md", confidence="medium"),
        match(source="d.md", confidence="high"),
    ])
    passages = [passage(source=s, chunk_id=s) for s in ("a.md", "b.md", "c.md", "d.md")]
    result = finalize(judgment, passages)
    assert [m.source_document for m in result.matches] == ["b.md", "d.md", "c.md", "a.md"]


def test_duplicate_matches_for_one_document_keep_the_most_confident():
    judgment = TypologyJudgment(matches=[match(confidence="low"), match(confidence="high")])
    result = finalize(judgment)
    assert [m.confidence for m in result.matches] == ["high"]
    assert any("duplicate" in w for w in result.warnings)


def test_detector_corroboration_is_set_by_code_from_the_rule_engine():
    passages = [passage(source=LAYERING_DOC, chunk_id="l"), passage(source=STRUCTURING_DOC, chunk_id="s"),
                passage(source=HAWALA_DOC, chunk_id="h")]
    judgment = TypologyJudgment(matches=[
        match(source=LAYERING_DOC), match(source=STRUCTURING_DOC), match(source=HAWALA_DOC),
    ])
    result = finalize(judgment, passages, rk=risk(layering=True, structuring=False))
    by_doc = {m.source_document: m.detector_corroborated for m in result.matches}
    assert by_doc == {LAYERING_DOC: True, STRUCTURING_DOC: False, HAWALA_DOC: None}


def test_the_llm_is_never_shown_the_code_only_field():
    assert "detector_corroborated" not in json.dumps(TypologyJudgment.model_json_schema())


def test_no_match_keeps_the_models_reason_or_supplies_a_default():
    assert finalize(TypologyJudgment(no_match_reason="Nothing fits.")).no_match_reason == "Nothing fits."
    assert finalize(TypologyJudgment()).no_match_reason == "No documented typology matched the evidence."


def test_the_passages_shown_are_kept_on_the_assessment_for_audit():
    result = finalize(TypologyJudgment(matches=[match()]), [passage(text="the passage", distance=0.31)])
    assert result.passages[0].text == "the passage" and result.passages[0].distance == 0.31


# ---------------------------------------------------------------------------- the graph
def test_happy_path_searches_each_query_then_judges_once():
    search, judge = FakeSearch([passage()]), FakeJudge()
    result = run(search=search, judge=judge, config=TypologyConfig(top_k_per_query=3))
    assert [k for _, k in search.calls] == [3, 3, 3]  # one search per pattern field
    assert len(judge.prompts) == 1
    assert result["assessment"].matches[0].source_document == LAYERING_DOC


def test_no_citations_means_no_search_and_no_llm_call():
    search, judge = FakeSearch([passage()]), FakeJudge()
    result = run(ev=evidence(citations={}), search=search, judge=judge)
    assert search.calls == [] and judge.prompts == []
    assert result["assessment"].no_match_reason == "No evidence available to match."


def test_nothing_close_enough_skips_the_llm_call():
    judge = FakeJudge()
    result = run(search=FakeSearch([passage(distance=0.95)]), judge=judge)
    assert judge.prompts == []
    assert "similar enough" in result["assessment"].no_match_reason


def test_a_missing_index_becomes_an_error_state_not_a_crash():
    judge = FakeJudge()
    result = run(search=FakeSearch(error=IndexNotBuiltError("build the index")), judge=judge)
    assert result["error"] == "build the index" and "assessment" not in result
    assert judge.prompts == []


# ------------------------------------------------------------------ match_typology()
def test_match_typology_checks_the_api_key_before_loading_the_embedding_model(monkeypatch):
    def forbidden():
        raise AssertionError("the embedding model was loaded before the API key was checked")

    monkeypatch.setattr(typology, "default_embedder", forbidden)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        match_typology(evidence(), risk(), settings=Settings(_env_file=None, llm_provider="anthropic"))


def test_prompt_sets_the_grounding_rules_found_necessary_in_live_testing():
    prompt = build_typology_prompt(evidence(), risk(), [passage()])
    assert "merely consistent with the flag" in prompt  # no stretching a flag the data cannot show
    assert "A missing fact is never support" in prompt  # silence is not evidence
    assert "context only" in prompt and "must not say or imply" in prompt  # profile is not a red flag


# ------------------------------------------------ messy evidence ids from the model
def test_a_comma_joined_id_string_is_split_into_real_ids():
    result = finalize(TypologyJudgment(matches=[match(ids=("TXN1, TXN2",))]))
    assert result.matches[0].evidence_ids == ["TXN1", "TXN2"] and result.warnings == []


def test_padded_and_repeated_ids_are_trimmed_and_deduplicated():
    result = finalize(TypologyJudgment(matches=[match(ids=(" TXN1", "TXN1 ", "TXN2"))]))
    assert result.matches[0].evidence_ids == ["TXN1", "TXN2"] and result.warnings == []


def test_the_unknown_id_warning_shows_each_value_with_repr_so_oddities_are_visible():
    result = finalize(TypologyJudgment(matches=[match(ids=("TXN1", "TXN999"))]))
    assert "'TXN999'" in result.warnings[0]


def test_a_sentence_containing_commas_is_left_whole_and_reported_intact():
    # Seen live: Claude put the rule engine's finding into evidence_ids. Splitting on its commas
    # produced garbage fragments; it must be reported as the single oddity it is.
    sentence = "structuring signal: 3 deposits $7,000-$10,000 in 90 days"
    result = finalize(TypologyJudgment(matches=[match(ids=("TXN1", sentence))]))
    assert result.matches[0].evidence_ids == ["TXN1"]
    assert f"'{sentence}'" in result.warnings[0]


def test_the_prompt_says_evidence_ids_are_ids_only():
    prompt = build_typology_prompt(evidence(), risk(), [passage()])
    assert "holds ids from the list provided and nothing else" in prompt


# ------------------------------------------------ verdict-wording tripwire
def match_with_rationale(text):
    return TypologyMatch(
        typology="Layering", source_document=LAYERING_DOC, confidence="high", rationale=text, evidence_ids=["TXN1"],
        matched_red_flags=["a red flag the evidence supports"],
    )


def test_verdict_like_wording_raises_a_warning_but_keeps_the_match():
    wording = "These are suspicious indicators of layering rather than legitimate commerce."
    result = finalize(TypologyJudgment(matches=[match_with_rationale(wording)]))
    assert len(result.matches) == 1  # never dropped or edited - only flagged for review
    assert result.matches[0].rationale == wording
    assert any("'suspicious'" in w and "Layering" in w for w in result.warnings)


def test_neutral_wording_raises_no_warning():
    result = finalize(TypologyJudgment(matches=[match_with_rationale("The chain completed in 17 hours.")]))
    assert result.warnings == []


def test_a_no_match_reason_is_not_scanned_because_it_is_usually_benign_negation():
    # Seen live: "the passages describe only suspicious patterns; this account matches none".
    result = finalize(TypologyJudgment(no_match_reason="It does not resemble any suspicious typology."))
    assert result.warnings == []


# ------------------------------------------- ids the rule engine flagged are always citable
def risk_with_ids(structuring_ids=(), layering_ids=()):
    return RiskScore(
        score=50, tier="medium", reasons=["flagged"],
        structuring=StructuringSignal(flagged=bool(structuring_ids), matched_transaction_ids=list(structuring_ids)),
        layering=LayeringSignal(flagged=bool(layering_ids), chain_transaction_ids=list(layering_ids)),
    )


def test_rule_engine_ids_are_citable_even_if_the_evidence_summary_never_cited_them():
    # The Evidence Agent's LLM-written citations left TXN7 out; the deterministic rule engine flagged it.
    ev = evidence(citations={"a wire": ["TXN1"]})
    judgment = TypologyJudgment(matches=[match(source=STRUCTURING_DOC, ids=("TXN7",))])
    result = finalize(judgment, [passage(source=STRUCTURING_DOC)], ev=ev, rk=risk_with_ids(structuring_ids=["TXN7"]))
    assert result.matches[0].evidence_ids == ["TXN7"] and result.warnings == []


def test_ids_from_a_signal_that_did_not_flag_are_not_citable():
    ev = evidence(citations={"a wire": ["TXN1"]})
    rk = risk_with_ids()
    rk.structuring = StructuringSignal(flagged=False, matched_transaction_ids=["TXN7"])  # not flagged: ignored
    result = finalize(TypologyJudgment(matches=[match(source=STRUCTURING_DOC, ids=("TXN7",))]),
                      [passage(source=STRUCTURING_DOC)], ev=ev, rk=rk)
    assert result.matches == []


def test_the_prompt_lists_the_rule_engines_flagged_ids_once_each():
    rk = risk_with_ids(structuring_ids=["TXN7", "TXN8"], layering_ids=["TXN8", "TXN9"])
    prompt = build_typology_prompt(evidence(), rk, [passage()])
    assert "Transaction ids flagged by the rule engine (also citable): TXN7, TXN8, TXN9" in prompt


def test_the_prompt_has_no_rule_engine_id_line_when_nothing_was_flagged():
    assert "flagged by the rule engine" not in build_typology_prompt(evidence(), risk_with_ids(), [passage()])


# ------------------------------------------- a match must rest on at least one supported red flag
def test_a_match_with_no_supported_red_flag_is_dropped_and_reported():
    weak = TypologyMatch(
        typology="Hawala", source_document=LAYERING_DOC, confidence="low", matched_red_flags=[],
        unassessable_red_flags=["every other flag"], rationale="one shared country", evidence_ids=["TXN1"],
    )
    result = finalize(TypologyJudgment(matches=[weak]))
    assert result.matches == []
    assert "Dropped match 'Hawala': none of its red flags is supported by the evidence" in result.warnings
    assert result.no_match_reason == "Every proposed match failed validation - see warnings."


def test_a_supported_match_is_kept_when_a_weak_one_is_dropped_beside_it():
    weak = TypologyMatch(typology="Weak", source_document=STRUCTURING_DOC, confidence="low", rationale="r", evidence_ids=["TXN1"])
    result = finalize(TypologyJudgment(matches=[match(), weak]), [passage(), passage(source=STRUCTURING_DOC)])
    assert [m.typology for m in result.matches] == ["Layering"]


def test_the_prompt_tells_the_model_to_report_only_supported_typologies():
    assert "Report a typology only if the evidence supports at least one of its red flags" in SYSTEM_PROMPT
