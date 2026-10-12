# Evaluation report - Gemini 3.1 Pro + 3.5 Flash on Vertex AI

21 accounts, 0 failed, 0 incomplete. Cost $3.36, about 152.57142857142858s per account.

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
- Narrative-agent warnings per draft: 0.8
- Faithfulness check ran on 15 drafts: **67%** passed outright; **97%** of 203 sentences supported (unsupported 2, contradicted 0, unverified 5, consistency issues 0)

## Per account

| Account | Expected | Got | Typologies | Draft | Faithful | Cost |
|---|---|---|---|---|---|---|
| ACC-N-00025 | low | low | none | no | - | $0.043 |
| ACC-N-00114 | low | low | none | no | - | $0.048 |
| ACC-N-00142 | low | low | none | no | - | $0.048 |
| ACC-N-00228 | low | low | none | no | - | $0.039 |
| ACC-N-00250 | low | low | none | no | - | $0.073 |
| ACC-N-00281 | low | low | none | no | - | $0.037 |
| ACC-S-00089 | medium | medium | structuring | yes | 1/15 flagged | $0.238 |
| ACC-S-00104 | medium | medium | structuring | yes | pass | $0.222 |
| ACC-S-00558 | medium | medium | structuring | yes | pass | $0.185 |
| ACC-S-00692 | medium | medium | structuring | yes | pass | $0.196 |
| ACC-S-00754 | medium | medium | structuring | yes | pass | $0.179 |
| ACC-S-00758 | medium | medium | structuring | yes | 1/12 flagged | $0.165 |
| ACC-L-00030 | medium | medium | rapid_wire_transfers | yes | pass | $0.189 |
| ACC-L-00032 | medium | medium | rapid_wire_transfers | yes | 3/11 flagged | $0.170 |
| ACC-L-00095 | medium | medium | rapid_wire_transfers | yes | pass | $0.189 |
| ACC-L-00223 | medium | medium | rapid_wire_transfers | yes | pass | $0.227 |
| ACC-L-00432 | medium | medium | rapid_wire_transfers | yes | 1/12 flagged | $0.185 |
| ACC-L-00604 | medium | medium | rapid_wire_transfers | yes | pass | $0.164 |
| ACC-H-00000 | high | high | rapid_wire_transfers, structuring | yes | pass | $0.275 |
| ACC-H-00001 | high | high | rapid_wire_transfers, structuring | yes | pass | $0.248 |
| ACC-H-00002 | high | high | rapid_wire_transfers, structuring | yes | 1/15 flagged | $0.240 |
