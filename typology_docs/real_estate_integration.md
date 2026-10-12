# Real Estate / Luxury Asset Integration

**Typology ID:** TYP-006
**Stage:** Integration
**Channels:** Property and luxury-goods purchases, often through a corporate or trust vehicle
**Detector:** Not yet built

## Definition

Integration is the final stage of laundering: money that has already been placed and layered
re-enters the economy looking like a legitimate asset. Real estate and luxury goods are among
the most common integration vehicles, because they hold or appreciate in value, can be bought
with minimal financing scrutiny when paid in cash, and — when purchased through a shell
company — can obscure the real buyer entirely. FATF guidance treats the real estate sector as
fully exposed to this risk, particularly in politically and economically stable countries
where property is a trusted long-term store of value.

## How it works

Layered funds purchase high-value property or luxury goods (real estate, art, yachts, jewelry),
often through a corporate vehicle rather than the beneficial owner's own name, frequently with
cash or minimal financing to avoid a lender's own due diligence. FinCEN's advisory on real
estate risk specifically flags all-cash purchases of luxury property through shell companies
as a pattern meriting scrutiny on its own. Once integrated, the asset can later be resold,
producing proceeds that appear to come from a legitimate sale rather than the original crime.

## Red flags

- All-cash purchase of high-value property with no financing
- Purchase made through a shell company, trust, or other entity obscuring the real buyer
- A buyer who is a Politically Exposed Person (PEP) acquiring property inconsistent with
  their declared income
- Property quickly resold after purchase with no clear economic or personal rationale

## Investigation questions

- Who is the ultimate buyer, and was a shell company, trust or other entity used to hold the asset?
- How was the purchase funded: all cash, or financed - and what is the source of those funds?
- Is the buyer a Politically Exposed Person, and does the purchase fit their declared income?
- Was the property resold quickly, and what was the reason?
- Does the value of the asset fit what is known about the customer's wealth?

## How this project's detector maps to it

No detector exists, and like shell companies, the missing piece isn't transaction logic — it's
data. This project's KYC profile already tracks `is_pep`, which is directly relevant input for
this typology, but there's no real-estate or asset-purchase data source at all. A future
detector could combine the existing `is_pep` flag with a hypothetical high-value-asset-purchase
feed, but that data source doesn't exist in this project's current three-source architecture.

## Sources

- [FinCEN — Advisory to Financial Institutions and Real Estate Firms (FIN-2017-A003)](https://www.fincen.gov/system/files/advisory/2017-08-22/Risk%20in%20Real%20Estate%20Advisory_FINAL%20508%20Tuesday%20(002).pdf)
- [FATF — risk-based approach guidance for the real estate sector](https://www.moneylaunderingnews.com/2022/08/fatf-updates-risk-based-approach-guidance-for-the-real-estate-sector/)
