# Hawala and Other Similar Service Providers (Informal Value Transfer)

**Typology ID:** TYP-007
**Stage:** Layering (sometimes placement, when cash first enters through an informal agent)
**Channels:** Informal value transfer agents; deposits followed by international wire transfers
**Detector:** Not yet built
**Primary source:** `fatf_hawala_hossp_2013.pdf` (full FATF report, kept in this folder for
the vector store to chunk directly — this summary is a distillation, not a replacement)

## Definition

Hawala and Other Similar Service Providers (HOSSPs) move money between countries on trust,
outside the banking system — a sender pays an agent in one country, and a paired agent in
another country pays the recipient, with the two agents settling their own balance later
through a separate channel. No money actually crosses the border for any individual transfer.
FATF's 2013 report — based on survey responses from 33 countries — found this to be a
genuinely two-sided reality: HOSSPs serve legitimate, often unbanked communities, while also
being flagged by law enforcement in many of the same jurisdictions as a leading channel for
laundering and terrorist financing.

## How it works

FATF's report distinguishes three types, each carrying different risk:

- **Pure traditional** HOSSPs — legitimate, trust-based networks serving communities with
  limited access to banking, typically along family, regional, or tribal lines.
- **Hybrid traditional** HOSSPs — legitimate on the surface, but used (knowingly or not) to
  move illicit funds across borders alongside genuine remittance traffic.
- **Criminal** HOSSPs — set up specifically to serve criminal clients.

Because settlement between agents happens separately from any individual transfer, there is
no direct wire or bank record linking a specific sender to a specific recipient — which is
exactly what makes the channel useful for layering money across borders without the paper
trail a bank wire would leave.

## Red flags

FATF's report lists transaction patterns financial institutions can actually observe, even
without direct insight into the HOSSP network itself (Section 3.6.4 of the source document).
The ones most relevant to this project's bank-transaction data:

- **Collective/funnel accounts:** many small deposits into one account, often followed by one
  aggregated transfer abroad
- **Frequent deposits by multiple individuals into a single account**, followed by
  international wire transfers or withdrawals
- **Transactions structured to fall just under identification, STR, or CTR thresholds**
- **Business accounts receiving or disbursing large sums with no normal business activity**
  (no payroll, no invoices)
- **Funds that sit in an account only briefly** before moving out again
- **Frequent wires to international destinations with no plausible business connection**,
  historically concentrated through major remittance hubs (the report specifically names
  Dubai as a common routing point)

## Investigation questions

- Are deposits coming from many different individuals into one account, and how are those individuals connected?
- Where do the funds go next: one aggregated transfer abroad, and to what destinations?
- How long do funds stay in the account before moving out?
- For a business account, is the normal business activity (payroll, invoices) missing?
- Is any counterparty a licensed money-service business or known remitter, and does the customer have a documented reason for using it?
- Do the amounts sit just under identification or reporting thresholds?

## How this project's detector maps to it

No detector exists, but this is a different kind of gap than shell companies or crypto:
several of the red flags above (funnel-style deposits, near-threshold amounts, rapid pass-
through) are *structurally* similar to what `structuring.py` and `layering.py` already catch
— this project's transaction schema just has no field identifying a counterparty as a money
service business or informal remittance agent, so there's no way to specifically attribute a
flagged pattern to hawala rather than to an ordinary rapid-transfer scheme. A future version
could get partial coverage for free by checking whether a flagged layering chain's final hop
lands at a known money-remitter account type — but that requires adding an entity-type field
to the account data this project doesn't currently have, the same limitation noted for shell
companies and real estate integration.

## Sources

- FATF, *The Role of Hawala and Other Similar Service Providers in Money Laundering and
  Terrorist Financing* (October 2013) — full report kept in this folder as
  `fatf_hawala_hossp_2013.pdf`
- [FinCEN — Advisory on Informal Value Transfer Systems (2010)](https://www.fincen.gov/resources/advisories/fincen-advisory-issue-4), referenced in the FATF report's Box 3.1
