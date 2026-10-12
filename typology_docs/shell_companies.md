# Shell Companies

**Typology ID:** TYP-004
**Stage:** Layering
**Channels:** Corporate account transfers; accounts held in a company's name
**Detector:** Not yet built

## Definition

A shell company is a legally incorporated entity with no significant real operations, staff,
or physical presence — it exists on paper to hold accounts and move money, not to run a
business. A joint FATF/Egmont Group study of 106 real case studies across 34 countries found
that shell companies were a central feature in the large majority of schemes built to conceal
who actually owns and controls laundered funds.

## How it works

A launderer registers one or more shell companies, often in jurisdictions with weak
beneficial-ownership disclosure requirements, and routes money through accounts held in the
company's name rather than their own. Layering money through a chain of shell companies —
sometimes nested, one owning another — adds legal and investigative distance between the
illicit source and the eventual destination, since each additional corporate layer requires a
separate legal process to pierce. FATF's beneficial ownership guidance specifically targets
this gap: without knowing the real human behind a company, a bank can't assess whether its
activity makes sense for who actually controls it.

## Red flags

- An account holder that is a company with no identifiable operations, website, employees,
  or physical address consistent with its stated business
- Beneficial ownership that is unclear, nominee-held, or routed through other corporate
  entities rather than a named individual
- Transaction activity with no plausible connection to the company's stated business purpose
- Rapid formation and dissolution of corporate entities, or multiple shell companies sharing
  the same registered agent, address, or beneficial owner

## Investigation questions

- Who is the real person behind the company, and is the ownership nominee-held or routed through other entities?
- What operations, employees, website or physical address does the company actually have?
- Does the account activity connect to the company's stated business purpose?
- Do other companies share the same registered agent, address or beneficial owner?
- When was the company formed, and is there a history of rapid formation or dissolution of related entities?
- Why was this jurisdiction or corporate structure chosen?

## How this project's detector maps to it

No detector exists yet, and this project's current data model has a real gap here: Salesforce
customer profiles have no field distinguishing an individual from a corporate entity, and no
beneficial-ownership data at all. Building this detector would require extending the KYC
schema first — it can't be bolted onto the existing transaction-pattern detectors the way
structuring and layering were, because the signal is about *who the account holder actually
is*, not what the transactions look like.

## Sources

- [FATF/Egmont Group — Concealment of Beneficial Ownership (July 2018)](https://www.fatf-gafi.org/content/dam/fatf-gafi/reports/FATF-Egmont-Concealment-beneficial-ownership.pdf)
- [FATF — Beneficial Ownership of Legal Persons (March 2023)](https://www.fatf-gafi.org/content/dam/fatf-gafi/guidance/Guidance-Beneficial-Ownership-Legal-Persons.pdf.coredownload.pdf)
