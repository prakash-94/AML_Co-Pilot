# Cash-Intensive Business Layering

**Typology ID:** TYP-003
**Stage:** Placement
**Channels:** Cash deposits made through a business account
**Detector:** Not yet built

## Definition

A cash-intensive business — a laundromat, car wash, parking lot, restaurant, bar, construction
firm, or similar business that legitimately handles large volumes of cash — is used to mix
illicit cash with real revenue. Because the business genuinely generates cash day to day, a
launderer can co-mingle dirty money with legitimate takings and deposit the blended total,
making the illicit portion far harder to isolate than a structuring pattern would be.

## How it works

The launderer owns or controls a business whose real-world cash volume is plausible but hard
to verify precisely — there's no independent way to know exactly how many cars a car wash
actually cleaned in a day. Illicit cash is folded into the daily till before deposit, so the
bank sees one lump sum that looks like ordinary business revenue. FATF typology work names
this placement channel as one of the most common used by organized crime groups specifically
because deposits through a legitimate-looking business attract far less scrutiny than cash
appearing with no business explanation at all.

## Red flags

- Deposited cash volume inconsistent with the business's physical capacity (e.g., a car wash
  depositing far more than its bays could plausibly process)
- Revenue that doesn't track with seasonal or economic patterns normal for that business type
- A business with minimal visible staff, inventory, or customer traffic relative to its
  reported cash revenue
- Rapid, repeated ownership changes or multiple cash businesses under one beneficial owner

## Investigation questions

- What does the business actually do, and how much cash could it plausibly handle given its physical capacity?
- Do deposits follow the seasonal and economic patterns normal for this type of business?
- How much visible staff, inventory and customer traffic does the business have relative to its reported cash revenue?
- Who owns or controls the business, and has ownership changed repeatedly or does one owner control several cash businesses?
- Can the reported revenue be corroborated by independent records such as sales systems, supplier invoices or tax filings?

## How this project's detector maps to it

No detector exists yet. A first version would need an expected-revenue baseline per
`occupation`/business type (not currently modeled in this project's Salesforce profile data)
to compare against actual deposited amounts — the hard part is that, unlike structuring, there
is no fixed numeric threshold to check against; it requires a plausibility judgment, which is
a natural candidate for an LLM-assisted check once the Typology/RAG Agent exists, rather than
a simple rule.

## Sources

- [FATF — cash-intensive businesses as a placement channel](https://www.fatf-gafi.org/en/publications/Methodsandtrends/quick-guide-on-assessing-ML-risks-of-the-informal-economy.html)
- [FATF — Global Money Laundering & Terrorist Financing Threat Assessment](https://www.fatf-gafi.org/content/dam/fatf-gafi/reports/Global%20Threat%20assessment.pdf)
