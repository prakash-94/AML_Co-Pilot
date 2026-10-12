# Structuring (Smurfing)

**Typology ID:** TYP-001
**Stage:** Placement
**Channels:** Cash deposits (branch, multiple branches); monetary instruments such as cashier's checks and money orders
**Detector:** `aml_copilot/detection/structuring.py`

## Definition

Structuring is breaking a single large sum of cash into multiple smaller deposits, each kept
under the $10,000 threshold that triggers a mandatory Currency Transaction Report (CTR),
specifically to avoid that report being filed. U.S. law treats structuring as a crime in its
own right, separate from whatever underlying activity generated the cash — a bank does not
need to prove the money's original source to suspect structuring; the evasion pattern itself
is the red flag.

## How it works

A person or group deposits cash in amounts just below the reporting threshold — often in the
$7,000–$9,999 range — repeatedly over time. To avoid an obvious pattern, the deposits are
frequently spread across:
- **Multiple days or weeks** rather than one visit
- **Multiple bank branches**, so no single teller sees the pattern
- **Multiple accomplices** ("smurfs"), each making deposits under their own name

Examiner guidance describes monitoring for exactly this shape: several lower-value cash
transactions that, aggregated over a period of time, add up to a much larger sum. The same
logic extends to structured purchases of monetary instruments — cashier's checks, money
orders, traveler's checks — bought just under the threshold.

## Red flags

- Multiple cash deposits each just under $10,000, from the same account or related accounts
- Deposits spread across different branches or days to avoid an obvious single-visit pattern
- A customer whose stated occupation or business doesn't plausibly generate this volume of cash
- Structured purchases of monetary instruments in the same near-threshold pattern

## Investigation questions

- What is the customer's expected source of cash, and does their stated occupation or business plausibly produce this volume?
- Are the near-threshold deposits spread across branches or days, and is there a business reason for that spread?
- Were the deposits all made by the account holder, or by other people - and are those people linked to each other or to the account?
- Where did the money go after the deposits: withdrawn, transferred out, or used to buy monetary instruments?
- Has the customer documented a legitimate explanation, and does it fit the actual dates and amounts?
- Do any prior alerts or cases on this account show the same pattern?

## How this project's detector maps to it

`detect_structuring()` looks for 3 or more cash deposits between $7,000 and just under
$10,000, scans for the largest time-clustered group using a 90-day window, and reports which
bank branches they occurred at. The 90-day window (rather than a tight few-day window) was a
deliberate, data-driven choice: live testing against this project's own seed data showed a
structuring account spreading its deposits 52 days apart across three branches — the exact
evasion behavior the regulatory guidance above describes — which a shorter window would have
missed entirely.

## Sources

- [FFIEC BSA/AML Examination Manual — Currency Transaction Reporting](https://bsaaml.ffiec.gov/manual)
- [FDIC — FFIEC BSA/AML Examination Manual, Currency Transaction Reporting](https://www.fdic.gov/news/financial-institution-letters/2021/fil21012c.pdf)
