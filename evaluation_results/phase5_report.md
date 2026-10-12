# Phase 5 evaluation - 21 accounts, seed 42

21 accounts, 0 failed, 0 incomplete. Cost $0.88.

## Detection and matching

- Tier accuracy: **100%**
- Typology match precision: **100%**, recall: **100%**
- False alarms on normal accounts: **0%**
- Flagged accounts that got no draft: **0%**

| Family | Accounts | Right tier | Exactly the right typologies | Right draft decision |
|---|---|---|---|---|
| H | 3 | 100% | 100% | 100% |
| L | 6 | 100% | 100% | 100% |
| N | 6 | 100% | 100% | 100% |
| S | 6 | 100% | 100% | 100% |

## Narrative quality

- Drafts written: 15; dropped for an invented figure/date/id: 0 sentences
- Narrative-agent warnings per draft: 0.2
- Faithfulness check ran on 15 drafts: **80%** passed outright; **99%** of 236 sentences supported (unsupported 0, contradicted 1, unverified 2, consistency issues 0)

## Per account

| Account | Expected | Got | Typologies | Draft | Faithful | Cost |
|---|---|---|---|---|---|---|
| ACC-N-00025 | low | low | none | no | - | $0.020 |
| ACC-N-00114 | low | low | none | no | - | $0.019 |
| ACC-N-00142 | low | low | none | no | - | $0.022 |
| ACC-N-00228 | low | low | none | no | - | $0.021 |
| ACC-N-00250 | low | low | none | no | - | $0.019 |
| ACC-N-00281 | low | low | none | no | - | $0.020 |
| ACC-S-00089 | medium | medium | structuring | yes | pass | $0.041 |
| ACC-S-00104 | medium | medium | structuring | yes | pass | $0.039 |
| ACC-S-00558 | medium | medium | structuring | yes | 1/18 flagged | $0.045 |
| ACC-S-00692 | medium | medium | structuring | yes | pass | $0.040 |
| ACC-S-00754 | medium | medium | structuring | yes | pass | $0.048 |
| ACC-S-00758 | medium | medium | structuring | yes | pass | $0.046 |
| ACC-L-00030 | medium | medium | rapid_wire_transfers | yes | pass | $0.042 |
| ACC-L-00032 | medium | medium | rapid_wire_transfers | yes | 1/13 flagged | $0.045 |
| ACC-L-00095 | medium | medium | rapid_wire_transfers | yes | 1/13 flagged | $0.042 |
| ACC-L-00223 | medium | medium | rapid_wire_transfers | yes | pass | $0.116 |
| ACC-L-00432 | medium | medium | rapid_wire_transfers | yes | pass | $0.045 |
| ACC-L-00604 | medium | medium | rapid_wire_transfers | yes | pass | $0.045 |
| ACC-H-00000 | high | high | rapid_wire_transfers, structuring | yes | pass | $0.057 |
| ACC-H-00001 | high | high | rapid_wire_transfers, structuring | yes | pass | $0.051 |
| ACC-H-00002 | high | high | rapid_wire_transfers, structuring | yes | pass | $0.053 |

## Testing the fact-checker itself (planted errors)

Each of the 15 real drafts was corrupted with known errors (an invented motive, an invented
relationship, an invented destination, a contradiction of the PEP status, and the direction of a real
wire reversed) while keeping every figure, date and id valid, so only a reader who understands the
sentence can object. The judge's job: flag exactly those, leave the 228 genuine sentences alone.

| Judge model | Planted errors caught | Genuine sentences wrongly questioned |
|---|---|---|
| Claude Sonnet 5 (final prompt, corrected test) | **68 of 68 (100%)** | **7 of 228 (3%)** |
| Claude Sonnet 5 (first prompt) | 62 of 68 | 21 of 228 (9%) |
| Claude Haiku 4.5 (first prompt) | 62 of 68 | 80 of 228 (35%) |

The six "missed" errors in the first two rows were a bug in the test, not the judge: for accounts whose
profile says "not a PEP" the test planted "is not a politically exposed person", which is true.
Haiku's recall had the same six, so its true recall is also likely 100%; it was not re-run. What
separates the models is false alarms: Haiku questions about a third of true sentences, which would
bury an investigator, so the faithfulness check runs on Sonnet 5.

## What the evaluation changed

1. The typology agent now drops a match that cites no supported red flag (one low-confidence hawala
   "match" on a layering account cost 5 points of precision) - and the prompt says so.
2. The fact-checker is shown which ids back each cited claim, and treats the required
   "the decision rests with the investigator" sentence as supported. Together these cut flagged
   genuine sentences from 10% to under 2% on the real drafts.
3. List fields a model sends as JSON text (seen live from both models) are parsed back instead of failing.

## Limits

- 21 accounts, one seed: enough to find real problems, not enough for confidence intervals.
- Expected answers come from the synthetic data's own design (the id prefix); a detector "miss" on a
  synthetic account may be a quirk of how that account was generated.
- The faithfulness check judges figures-in-meaning against the evidence summary, not against the raw
  database rows. If the Evidence Agent's summary is wrong, a faithful narrative inherits the error.
- The judge is not perfect. In `sample_runs/9_fact_check_flagged_ACC-L-00095.txt` its written reason ends
  "... this is actually supported" while its verdict says CONTRADICTED. Nothing in code can catch a verdict that
  disagrees with its own reasoning, which is why a flagged sentence is shown to the investigator and never
  deleted or rewritten automatically.
- Cost of the whole evaluation: about $2.63 (pipeline $0.88, judge testing $1.30, fact-checking $0.46).
