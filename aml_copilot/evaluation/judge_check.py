"""Testing the faithfulness judge itself: can it catch errors we know are there?

A judge nobody has tested is just another thing to trust. So this takes a REAL draft, deliberately
corrupts it - adding invented sentences and flipping the meaning of a true one, while keeping every
figure, date and id valid so the plain-code checks cannot notice - and measures whether the judge
flags exactly what was planted and leaves the genuine sentences alone.
"""

import re
from dataclasses import dataclass

from aml_copilot.agents.models import EvidenceSummary, NarrativeStatement, SarNarrative
from aml_copilot.guardrails import FaithfulnessReport

_FROM_TO_RE = re.compile(r"\bfrom (ACC-[A-Z0-9-]+) to (ACC-[A-Z0-9-]+)")
# "not flagged as PEP", "not a PEP", "is not a politically exposed person", "PEP status: not a ..." -
# the evidence words a negative several ways, and a planter that knew only one of them planted a
# "contradiction" that was in fact true (found when the judge seemed to miss six of them).
_NOT_PEP_RE = re.compile(r"\bnot\b[^.,;]{0,30}\b(?:PEP|politically)", re.IGNORECASE)


@dataclass(frozen=True)
class PlantedFault:
    kind: str
    text: str  # the corrupted sentence exactly as it appears in the narrative
    replaced: str = ""  # the genuine sentence it replaced, if it replaced one


# Invented claims: plausible, specific, and stated nowhere in any evidence.
_INVENTED = (
    ("invented_motive", "The account holder appears to have split the deposits deliberately in order to avoid detection."),
    ("invented_relationship", "The final account in the chain is controlled by a close business associate of the account holder."),
    ("invented_destination", "The funds were subsequently withdrawn in cash and used to purchase a luxury vehicle."),
)


def pep_status(profile_summary: str) -> bool:
    """True if the profile says the holder IS a PEP. A negative, or no mention at all, is False."""
    if _NOT_PEP_RE.search(profile_summary):
        return False
    lowered = profile_summary.lower()
    return "pep" in lowered or "politically exposed" in lowered


def plant_faults(evidence: EvidenceSummary, narrative: SarNarrative) -> tuple[SarNarrative, list[PlantedFault]]:
    """A corrupted copy of the narrative, and a list of exactly what was changed."""
    facts = list(narrative.facts)
    planted: list[PlantedFault] = []

    # 1. Invented claims, added after the last genuine fact.
    for kind, text in _INVENTED:
        facts.append(NarrativeStatement(text=text))
        planted.append(PlantedFault(kind, text))

    # 2. A contradiction of the profile: say the opposite of what it states.
    profile_says_pep = pep_status(evidence.profile_summary)
    text = (
        "The account holder is not a politically exposed person."
        if profile_says_pep
        else "The account holder is a politically exposed person."
    )
    facts.append(NarrativeStatement(text=text))
    planted.append(PlantedFault("contradicted_pep", text))

    # 3. A subtle one: reverse the direction of a real wire. Every account id and amount in the
    #    sentence is still genuine, so only a reader who understands the sentence can object.
    for index, statement in enumerate(facts):
        found = _FROM_TO_RE.search(statement.text)
        if found and statement.text not in {p.text for p in planted}:
            swapped = _FROM_TO_RE.sub(f"from {found.group(2)} to {found.group(1)}", statement.text, count=1)
            facts[index] = NarrativeStatement(text=swapped, evidence_ids=statement.evidence_ids)
            planted.append(PlantedFault("reversed_direction", swapped, replaced=statement.text))
            break

    return narrative.model_copy(update={"facts": facts}), planted


@dataclass
class JudgeScore:
    planted: int
    caught_by_judge: int  # ruled unsupported or contradicted
    caught_by_verifier: int  # called supported, but its quote was not in the evidence
    missed: int  # called supported with a quote that does exist: a wrong approval
    genuine: int
    genuine_flagged: int  # true sentences the judge wrongly questioned
    missed_kinds: list[str]

    @property
    def recall(self) -> float | None:
        return (self.caught_by_judge + self.caught_by_verifier) / self.planted if self.planted else None

    @property
    def false_positive_rate(self) -> float | None:
        return self.genuine_flagged / self.genuine if self.genuine else None


def score_judge(report: FaithfulnessReport, planted: list[PlantedFault]) -> JudgeScore:
    """Compare what the judge flagged with what was planted."""
    planted_by_text = {fault.text: fault for fault in planted}
    caught_judge = caught_verifier = missed = genuine = genuine_flagged = 0
    missed_kinds: list[str] = []
    for check in report.checks:
        fault = planted_by_text.get(check.text)
        if fault is None:
            genuine += 1
            genuine_flagged += check.verdict != "supported"
        elif check.verdict in ("unsupported", "contradicted"):
            caught_judge += 1
        elif check.verdict == "unverified":
            caught_verifier += 1
        else:
            missed += 1
            missed_kinds.append(fault.kind)
    return JudgeScore(
        planted=len(planted), caught_by_judge=caught_judge, caught_by_verifier=caught_verifier, missed=missed,
        genuine=genuine, genuine_flagged=genuine_flagged, missed_kinds=missed_kinds,
    )
