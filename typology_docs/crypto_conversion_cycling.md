# Crypto Conversion Cycling

**Typology ID:** TYP-005
**Stage:** Layering
**Channels:** Cryptocurrency exchanges and wallets; fiat-to-crypto and crypto-to-fiat conversions
**Detector:** Not yet built

## Definition

Crypto conversion cycling launders money by repeatedly converting between fiat currency and
convertible virtual currency (CVC) — and often between several different cryptocurrencies —
to break the audit trail a purely fiat transfer chain would leave. FinCEN's advisory on
illicit CVC activity catalogs this alongside the broader set of crimes CVC has been used to
facilitate, including fraud, extortion, and sanctions evasion, and names "mixing" services —
which deliberately blend many users' coins together — as a specific, named concern serious
enough that FinCEN has since moved to regulate CVC mixing directly as a class of transactions
of primary money-laundering concern.

## How it works

Funds enter a cryptocurrency exchange, get converted to one or more coins, moved through
multiple wallets (sometimes via a mixing service that deliberately obscures which coins came
from which depositor), converted again — potentially to a different cryptocurrency entirely —
and eventually cashed back out to fiat at a different exchange, often under a different
identity or jurisdiction than where the funds entered. Each conversion and each wallet hop
adds a link that's harder to trace than the last, especially across exchanges with weaker
know-your-customer controls.

## Red flags

- Rapid movement of funds between multiple wallets with no clear economic purpose
- Use of mixing or tumbling services, or privacy-focused coins, with no legitimate business
  rationale
- Cash-outs to fiat at a different exchange, in a different jurisdiction, under different
  identifying information than where funds entered
- Transaction patterns inconsistent with normal investment or trading behavior — rapid in,
  rapid out, with no holding period

## Investigation questions

- Which exchanges and wallets are involved, and who controls them?
- Is there a business or investment reason for funds moving rapidly in and out with no holding period?
- Was a mixing or tumbling service, or a privacy-focused coin, used, and why?
- Was the cash-out to fiat made at a different exchange, in a different jurisdiction, or under different identifying information - and is it the same person?
- Does the pattern fit the customer's known trading or investment history?

## How this project's detector maps to it

No detector exists, and this project's current transaction schema has no cryptocurrency or
wallet data at all — the `type` field only distinguishes `cash_deposit`, `card`, and
`wire_transfer`. Supporting this typology would require a new data source entirely (exchange
or blockchain transaction records), which is out of scope for the current three-source
architecture (Databricks, Salesforce, Supabase) unless a fourth source were added later.

## Sources

- [FinCEN — Advisory on Illicit Activity Involving Convertible Virtual Currency (2019)](https://www.fincen.gov/system/files/advisory/2019-05-10/FinCEN%20Advisory%20CVC%20FINAL%20508.pdf)
- [Federal Register — Proposed rule on CVC mixing as a primary money-laundering concern (2023)](https://www.federalregister.gov/documents/2023/10/23/2023-23449/proposal-of-special-measure-regarding-convertible-virtual-currency-mixing-as-a-class-of-transactions)
