# Rapid Wire Transfers Through Multiple Accounts (Layering)

**Typology ID:** TYP-002
**Stage:** Layering
**Channels:** Wire transfers; account-to-account transfers
**Detector:** `aml_copilot/detection/layering.py`

## Definition

Layering disguises the origin of illicit funds by moving them rapidly through a chain of
accounts — often at multiple institutions, sometimes across borders — before they settle
anywhere long enough to be traced back to the original deposit. FinCEN guidance notes that
wire transfers are a primary tool at every stage of laundering, and that complex layering
often involves multiple accounts held by multiple people, businesses, or shell companies in
quick succession.

## How it works

Money enters the financial system (via placement, often structuring), then moves through a
sequence of accounts in a short time window — frequently hours to a few days apart —
sometimes slightly reducing in amount at each hop as transfer fees or deliberate "cleaning"
cuts are taken. The related pattern of "funnel accounts" is explicitly named in FinCEN
guidance: cash is deposited in one geographic area and withdrawn in a different one, with
very little time elapsing between the two, specifically to obscure the money's path.

## Red flags

- A chain of transfers between related accounts completed in a short span (hours to days)
- Transfer amounts that shrink slightly at each hop, consistent with sequential "cleaning"
- A chain that crosses jurisdictions or currencies with no clear business rationale
- Deposit and withdrawal locations geographically distant with minimal time between them

## Investigation questions

- Who is the first known source of the funds, and where did they finally end up?
- Do the transfers correspond to invoices, payroll, or other documented activity?
- Why does the chain cross jurisdictions or currencies, and does the customer have a reason to be involved there?
- Do the accounts in the chain share owners, addresses, or other links?
- Why do the amounts shrink at each hop, and are the differences explained by documented fees?
- Is rapid movement normal for this customer (for example treasury, payroll or settlement activity)?
- Were funds deposited and withdrawn in geographically distant places within a short time, and why?

## How this project's detector maps to it

`detect_layering()` takes the chain of accounts found by network expansion
(`aml_copilot/ingestion/expansion.py`), checks it completed within a configurable rapid-transfer
window (72 hours by default), and reports two of the exact signals named above: whether the
transfer amounts shrink at each hop, and whether the chain crosses a border (the closest
available stand-in for cross-currency, since this project's transaction data has no currency
field). Live-verified against a real 3-hop chain in this project's own data: $44,587.98 →
$40,010.80 → $35,894.25 across three linked accounts, completed in 17 hours.

## Sources

- [FinCEN — wire transfers and layering through multiple accounts](https://www.fincen.gov/resources/advisories/fincen-advisory-issue-4)
- [FinCEN — funnel account activity](https://www.fincen.gov/sites/default/files/advisory/FIN-2012-A006.pdf)
