"""A malformed LLM answer must degrade into a clean error, never a crash.

The first test reproduces the exact failure seen live: with a data source down, Claude answered
the list field `data_completeness` with one sentence instead of a list of sentences.
"""

import pytest

from aml_copilot.agents.evidence import build_evidence_graph
from aml_copilot.agents.llm import invoke_structured
from aml_copilot.agents.models import EvidenceSummary, TypologyJudgment, TypologyMatch
from aml_copilot.agents.typology import build_typology_graph
from aml_copilot.detection import RiskScore
from aml_copilot.errors import ModelOutputError
from aml_copilot.ingestion import EvidenceBundle
from aml_copilot.retrieval import Chunk
from aml_copilot.retrieval.store import RetrievedChunk

ACC = "ACC-L-00000"
NO_SIGNAL = RiskScore(score=0, tier="low", reasons=["none"])


def evidence_fields(**overrides):
    base = {
        "account_id": ACC, "profile_summary": "p", "transaction_summary": "t",
        "network_summary": "n", "case_history_summary": "c", "detection_summary": "d",
    }
    return {**base, **overrides}


# -------------------------------------------------- lenient list-of-strings fields
def test_a_bare_sentence_for_data_completeness_becomes_a_one_item_list():
    summary = EvidenceSummary(**evidence_fields(data_completeness="Databricks was unavailable."))
    assert summary.data_completeness == ["Databricks was unavailable."]


def test_an_empty_string_becomes_an_empty_list():
    assert EvidenceSummary(**evidence_fields(data_completeness="  ")).data_completeness == []


def test_real_lists_are_left_alone():
    summary = EvidenceSummary(**evidence_fields(data_completeness=["a", "b"]))
    assert summary.data_completeness == ["a", "b"]


def test_the_typology_match_list_fields_are_lenient_too():
    m = TypologyMatch(
        typology="T", source_document="d.md", confidence="low", rationale="r",
        evidence_ids="TXN1", matched_red_flags="one flag", unassessable_red_flags="",
    )
    assert m.evidence_ids == ["TXN1"] and m.matched_red_flags == ["one flag"] and m.unassessable_red_flags == []


def test_leniency_does_not_change_the_schema_the_llm_sees():
    props = EvidenceSummary.model_json_schema()["properties"]["data_completeness"]
    assert props["type"] == "array" and props["items"] == {"type": "string"}


# ------------------------------------------------------------- invoke_structured
class BrokenChain:
    """Stands in for a structured-output chain whose answer fails schema validation."""

    def invoke(self, prompt):
        return TypologyJudgment.model_validate({"matches": [{"typology": "T"}]})  # fields missing


def test_a_schema_mismatch_becomes_a_model_output_error():
    with pytest.raises(ModelOutputError, match="did not match the required schema at 'matches.0"):
        invoke_structured(BrokenChain(), "prompt")


def test_a_schema_slip_is_retried_and_the_second_answer_is_used():
    class SlipsOnce:
        calls = 0

        def invoke(self, prompt):
            SlipsOnce.calls += 1
            if SlipsOnce.calls == 1:
                return TypologyJudgment.model_validate({"matches": [{"typology": "T"}]})  # fields missing
            return "second answer"

    assert invoke_structured(SlipsOnce(), "prompt") == "second answer"
    assert SlipsOnce.calls == 2


def test_a_persistent_schema_slip_is_tried_the_set_number_of_times_then_reported():
    class AlwaysBroken(BrokenChain):
        calls = 0

        def invoke(self, prompt):
            AlwaysBroken.calls += 1
            return super().invoke(prompt)

    with pytest.raises(ModelOutputError, match="did not match the required schema"):
        invoke_structured(AlwaysBroken(), "prompt", attempts=3)
    assert AlwaysBroken.calls == 3


def test_errors_other_than_a_schema_slip_are_not_retried():
    class Down:
        calls = 0

        def invoke(self, prompt):
            Down.calls += 1
            raise ConnectionError("network down")

    with pytest.raises(ConnectionError):
        invoke_structured(Down(), "prompt")
    assert Down.calls == 1


def test_a_good_answer_passes_straight_through():
    class GoodChain:
        def invoke(self, prompt):
            return "the answer"

    assert invoke_structured(GoodChain(), "prompt") == "the answer"


# ------------------------------------------------- the graphs record it, not crash
def raise_model_error(prompt):
    raise ModelOutputError("bad shape")


def test_evidence_agent_records_a_model_output_error_in_state():
    bundle = EvidenceBundle(account_id=ACC, transactions=[{"transaction_id": "t1", "account_id": ACC, "amount": 5}])
    graph = build_evidence_graph(lambda acc: bundle, lambda b: NO_SIGNAL, raise_model_error)
    result = graph.invoke({"account_id": ACC})
    assert result["error"] == "bad shape" and "summary" not in result


def test_typology_agent_records_a_model_output_error_in_state():
    evidence = EvidenceSummary(**evidence_fields(citations={"a claim": ["TXN1"]}))
    near = RetrievedChunk(chunk=Chunk(id="x", text="t", source="structuring.md", heading="H"), distance=0.3)
    graph = build_typology_graph(lambda q, k: [near], raise_model_error)
    result = graph.invoke({"evidence": evidence, "risk_score": NO_SIGNAL})
    assert result["error"] == "bad shape" and "assessment" not in result


# ---------------------------------------------- the chat model is built per model's rules
@pytest.fixture
def captured(monkeypatch):
    calls = []

    class FakeChatAnthropic:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", FakeChatAnthropic)
    return calls


def settings_for(model):
    from pydantic import SecretStr

    from aml_copilot.config import Settings

    return Settings(_env_file=None, anthropic_api_key=SecretStr("sk-test"), anthropic_model=model)


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-sonnet-4-6"])
def test_models_known_to_accept_temperature_get_temperature_zero(captured, model):
    from aml_copilot.agents.llm import make_chat_model

    make_chat_model(settings_for(model))
    assert captured[0]["temperature"] == 0


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-5", "some-future-model"])
def test_other_models_are_called_without_temperature_because_they_reject_it(captured, model):
    from aml_copilot.agents.llm import make_chat_model

    make_chat_model(settings_for(model))
    assert "temperature" not in captured[0]


def test_max_tokens_is_passed_only_when_asked_for(captured):
    from aml_copilot.agents.llm import make_chat_model

    make_chat_model(settings_for("claude-haiku-4-5"))
    make_chat_model(settings_for("claude-haiku-4-5"), max_tokens=4096)
    assert "max_tokens" not in captured[0] and captured[1]["max_tokens"] == 4096


def test_a_model_override_beats_the_settings_model_and_follows_its_own_temperature_rule(captured):
    from aml_copilot.agents.llm import make_chat_model

    make_chat_model(settings_for("claude-haiku-4-5"), model="claude-sonnet-5")
    assert captured[0]["model"] == "claude-sonnet-5" and "temperature" not in captured[0]


def test_the_typology_judge_uses_typology_model_when_set_else_the_default(monkeypatch):
    from aml_copilot.agents.typology import make_anthropic_judge

    class FakeChat:
        def with_structured_output(self, schema):
            return None

    models_asked_for = []

    def fake_make_chat_model(settings, max_tokens=None, model=None):
        models_asked_for.append(model)
        return FakeChat()

    monkeypatch.setattr("aml_copilot.agents.typology.make_chat_model", fake_make_chat_model)
    haiku = settings_for("claude-haiku-4-5")
    make_anthropic_judge(haiku)
    make_anthropic_judge(haiku.model_copy(update={"typology_model": "claude-sonnet-5"}))
    assert models_asked_for == [None, "claude-sonnet-5"]  # None = fall back to anthropic_model


def test_a_missing_key_fails_before_any_model_is_built(captured):
    from aml_copilot.agents.llm import make_chat_model
    from aml_copilot.config import Settings
    from aml_copilot.errors import ConfigError

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        make_chat_model(Settings(_env_file=None))
    assert captured == []


# ------------------------------------------ a list that arrives as JSON text is accepted
def test_a_list_field_sent_as_json_text_is_parsed_back_into_a_list():
    from aml_copilot.agents.models import NarrativeText

    as_text = '[{"text": "One fact.", "evidence_ids": ["TXN1"]}]'
    narrative = NarrativeText.model_validate({"introduction": as_text, "facts": as_text, "conclusion": as_text})
    assert narrative.facts[0].text == "One fact." and narrative.facts[0].evidence_ids == ["TXN1"]


def test_text_that_is_not_a_json_list_is_still_rejected_not_mangled():
    from pydantic import ValidationError

    from aml_copilot.agents.models import NarrativeText

    for bad in ("not json at all", '{"text": "an object, not a list"}', "[1, 2"):
        with pytest.raises(ValidationError):
            NarrativeText.model_validate({"introduction": bad, "facts": bad, "conclusion": bad})


def test_the_json_text_leniency_does_not_change_the_schema_the_model_sees():
    from aml_copilot.agents.models import NarrativeText, TypologyJudgment
    from aml_copilot.guardrails import FaithfulnessJudgment

    for model, field in ((NarrativeText, "facts"), (TypologyJudgment, "matches"), (FaithfulnessJudgment, "statements")):
        assert model.model_json_schema()["properties"][field]["type"] == "array"


def test_the_schema_error_says_what_kind_of_thing_the_model_sent():
    class SendsGarbage:
        def invoke(self, prompt):
            return TypologyJudgment.model_validate({"matches": "definitely not a list"})

    with pytest.raises(ModelOutputError, match=r"it sent a str: 'definitely not a list'"):
        invoke_structured(SendsGarbage(), "prompt")


def test_the_whole_answer_sent_as_json_text_inside_one_field_is_unwrapped_by_the_fields_name():
    from aml_copilot.guardrails import FaithfulnessJudgment

    wrapped = '{"statements":[\n{"number":1,"verdict":"supported","evidence_quote":"q","reason":"r"}]}'
    judgment = FaithfulnessJudgment.model_validate({"statements": wrapped})
    assert [s.number for s in judgment.statements] == [1]


def test_a_json_object_without_the_fields_own_key_is_still_rejected():
    from pydantic import ValidationError

    from aml_copilot.guardrails import FaithfulnessJudgment

    with pytest.raises(ValidationError):
        FaithfulnessJudgment.model_validate({"statements": '{"something_else": [1, 2]}'})
