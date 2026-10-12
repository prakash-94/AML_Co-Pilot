"""Add three HIGH-risk demo accounts (ACC-H-00000 .. ACC-H-00002) to all three source systems.

Why: the risk score adds 50 points for a structuring signal and 50 for a layering signal, and the
tier is high at 70+. Every existing demo account shows ONE pattern (medium) or none (low), so no
account could reach "high". These accounts show BOTH patterns:

  * structuring - three cash deposits just under $10,000, spread over ~3 weeks and 3 branches
  * layering    - then a 3-hop wire chain with shrinking amounts, crossing borders, in ~13 hours

It is ADDITIVE ONLY: it never changes or deletes an existing record, and it refuses to run if any
of its own ids already exist. Everything it adds can be removed again with --remove.

    python seed_high_risk_accounts.py             # dry run: print what WOULD be added, change nothing
    python seed_high_risk_accounts.py --apply     # add the records to Databricks, Salesforce, Supabase
    python seed_high_risk_accounts.py --remove    # delete exactly the records this script added

Credentials come from .env through the project's Settings; nothing is printed that is secret.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from aml_copilot.config import Settings

FIRST_TXN_NUMBER = 99_482  # the Databricks table currently ends at TXN099481
TXN_COLUMNS = (
    "transaction_id", "account_id", "timestamp", "amount", "type",
    "counterparty_account", "branch_id", "country", "ground_truth_label",
)
CASE_COLUMNS = (
    "case_id", "account_id", "case_type", "opened_date", "closed_date",
    "status", "disposition", "investigator_notes",
)


@dataclass(frozen=True)
class HighRiskAccount:
    number: int
    name: str
    occupation: str
    is_pep: bool
    kyc_reviewed: str
    deposits: tuple  # (timestamp, amount, branch) - each near, but under, $10,000
    card: tuple  # (timestamp, amount, branch) - an ordinary purchase, labelled normal
    wire_start: str  # when the first wire of the chain leaves the account
    wire_amounts: tuple  # shrinking at each hop
    wire_gaps_hours: tuple  # hours from hop 1 to hop 2, and from hop 2 to hop 3
    wire_countries: tuple  # where each hop happened
    wire_branches: tuple
    cases: tuple  # (case_type, opened, closed, disposition, status, notes) - prior alerts


ACCOUNTS = (
    HighRiskAccount(
        number=0, name="Wei Chen", occupation="Real estate agent", is_pep=True, kyc_reviewed="2025-09-20",
        deposits=(("2026-07-02 10:15:00", 9420.50, "BR001"), ("2026-07-14 15:40:00", 9180.75, "BR003"),
                  ("2026-07-26 11:05:00", 9310.20, "BR005")),
        card=("2026-07-19 18:22:00", 64.90, "BR002"),
        wire_start="2026-08-03 09:12:00", wire_amounts=(27805.30, 24990.12, 22410.77), wire_gaps_hours=(4, 9),
        wire_countries=("US", "PA", "AE"), wire_branches=("BR002", "BR004", "BR005"),
        cases=(
            ("Structuring Alert", "2025-03-11", "2025-04-02", "Escalated - Under Review", "Closed",
             "Escalated to senior investigator for further typology review."),
            ("Unusual Wire Pattern", "2025-06-20", "2025-07-15", "SAR Filed", "Closed",
             "Cross-referenced with prior alerts on linked accounts."),
            ("High-Risk Country Transfer", "2025-09-08", None, None, "Open", None),
        ),
    ),
    HighRiskAccount(
        number=1, name="Fatima Al-Farsi", occupation="Consultant", is_pep=False, kyc_reviewed="2025-11-14",
        deposits=(("2026-06-18 14:20:00", 9680.00, "BR002"), ("2026-07-03 09:45:00", 9540.35, "BR004"),
                  ("2026-07-21 16:10:00", 9725.60, "BR001")),
        card=("2026-07-10 12:05:00", 112.40, "BR003"),
        wire_start="2026-07-28 14:05:00", wire_amounts=(28610.40, 25740.15, 23115.90), wire_gaps_hours=(3, 11),
        wire_countries=("US", "KY", "PA"), wire_branches=("BR001", "BR003", "BR004"),
        cases=(
            ("Large Cash Transaction", "2025-02-04", "2025-02-27", "No Action - False Positive", "Closed",
             "Customer contacted for clarification, documentation provided satisfactorily."),
            ("Rapid Fund Movement", "2025-08-19", "2025-09-30", "SAR Filed", "Closed",
             "Escalated to senior investigator for further typology review."),
            ("Structuring Alert", "2025-10-27", None, None, "Open", None),
        ),
    ),
    HighRiskAccount(
        number=2, name="Carlos Rodriguez", occupation="Freelance contractor", is_pep=False,
        kyc_reviewed="2025-07-30",
        deposits=(("2026-06-25 11:30:00", 8940.80, "BR003"), ("2026-07-09 13:55:00", 9060.25, "BR005"),
                  ("2026-07-30 10:20:00", 8875.45, "BR002")),
        card=("2026-07-15 20:40:00", 48.15, "BR001"),
        wire_start="2026-08-06 08:30:00", wire_amounts=(26540.00, 23870.55, 21205.20), wire_gaps_hours=(5, 8),
        wire_countries=("US", "AE", "KY"), wire_branches=("BR004", "BR001", "BR003"),
        cases=(
            ("Velocity Alert", "2024-11-12", "2024-12-09", "Closed - Insufficient Evidence", "Closed",
             "Insufficient supporting evidence to substantiate suspicion at this time."),
            ("Round-Dollar Transaction Pattern", "2025-05-06", "2025-06-02", "Escalated - Under Review", "Closed",
             "Flagged for enhanced monitoring going forward."),
            ("Unusual Wire Pattern", "2025-11-03", None, None, "Open", None),
        ),
    ),
)


# ----------------------------------------------------------------- the plan (pure, no I/O)
def account_id(account: HighRiskAccount) -> str:
    return f"ACC-H-{account.number:05d}"


def chain_id(account: HighRiskAccount, hop: int) -> str:
    return f"ACC-HC-{account.number:05d}-{hop}"


def build_plan(accounts=ACCOUNTS, first_txn_number: int = FIRST_TXN_NUMBER) -> dict:
    """Every record this script adds, as plain data. Deterministic: same input, same ids."""
    transactions: list[tuple] = []
    profiles: list[dict] = []
    cases: list[tuple] = []
    txn_number = first_txn_number
    case_number = 1

    def next_txn() -> str:
        nonlocal txn_number
        txn_id = f"TXN{txn_number:06d}"
        txn_number += 1
        return txn_id

    for account in accounts:
        acc = account_id(account)
        for timestamp, amount, branch in account.deposits:  # structuring: labelled 'structuring'
            transactions.append((next_txn(), acc, timestamp, amount, "cash_deposit", None, branch, "US", "structuring"))
        timestamp, amount, branch = account.card  # an ordinary purchase, like the real structuring accounts have
        transactions.append((next_txn(), acc, timestamp, amount, "card", None, branch, "US", "normal"))

        # layering: acc -> HC-0 -> HC-1 -> HC-2, each wire sent by the account that received the last one
        start = datetime.fromisoformat(account.wire_start)
        times = [start, start + timedelta(hours=account.wire_gaps_hours[0])]
        times.append(times[1] + timedelta(hours=account.wire_gaps_hours[1]))
        senders = [acc, chain_id(account, 0), chain_id(account, 1)]
        receivers = [chain_id(account, 0), chain_id(account, 1), chain_id(account, 2)]
        for hop in range(3):
            transactions.append((
                next_txn(), senders[hop], times[hop].strftime("%Y-%m-%d %H:%M:%S"), account.wire_amounts[hop],
                "wire_transfer", receivers[hop], account.wire_branches[hop], account.wire_countries[hop], "layering",
            ))

        profiles.append({
            "Name": account.name, "Account_Id__c": acc, "Occupation__c": account.occupation,
            "Risk_Rating__c": "High", "Is_PEP__c": account.is_pep, "KYC_Last_Reviewed__c": account.kyc_reviewed,
        })
        for case_type, opened, closed, disposition, status, notes in account.cases:
            cases.append((f"CASE-H-{case_number:06d}", acc, case_type, opened, closed, status, disposition, notes))
            case_number += 1

    return {"transactions": transactions, "profiles": profiles, "cases": cases}


def describe(plan: dict) -> str:
    first_txn, last_txn = plan["transactions"][0][0], plan["transactions"][-1][0]
    lines = [f"Databricks  : {len(plan['transactions'])} transaction rows ({first_txn} .. {last_txn})",
             f"Salesforce  : {len(plan['profiles'])} KYC profiles",
             f"Supabase    : {len(plan['cases'])} prior cases", ""]
    for profile in plan["profiles"]:
        acc = profile["Account_Id__c"]
        lines.append(f"{acc}  {profile['Name']}, {profile['Occupation__c']}, PEP={profile['Is_PEP__c']}")
        for row in plan["transactions"]:
            if row[1] == acc or (row[1].startswith("ACC-HC-") and row[1][7:12] == acc[6:]):
                other = f" -> {row[5]}" if row[5] else ""
                lines.append(f"    {row[0]}  {row[2]}  {row[4]:13}  ${row[3]:>10,.2f}  {row[7]} {row[6]}  "
                             f"[{row[8]}]  {row[1]}{other}")
    return "\n".join(lines)


# --------------------------------------------------------------------- the three systems
def _databricks(settings: Settings):
    from databricks import sql

    return sql.connect(
        server_hostname=settings.databricks_server_hostname,
        http_path=settings.databricks_http_path,
        access_token=settings.databricks_token.get_secret_value(),
    )


def _salesforce(settings: Settings):
    from simple_salesforce import Salesforce

    return Salesforce(
        username=settings.sf_username,
        password=settings.sf_password.get_secret_value(),
        security_token=settings.sf_security_token.get_secret_value(),
    )


def _supabase(settings: Settings):
    import psycopg2

    return psycopg2.connect(settings.supabase_db_url.get_secret_value(), connect_timeout=10)


def _insert_transactions(settings: Settings, rows: list[tuple]) -> None:
    table = settings.databricks_transactions_table
    params: dict = {}
    value_rows = []
    for i, row in enumerate(rows):
        marks = []
        for j, value in enumerate(row):
            if value is None:
                marks.append("NULL")  # a SQL NULL, not a bound parameter
            else:
                params[f"r{i}c{j}"] = value
                marks.append(f":r{i}c{j}")
        value_rows.append("(" + ", ".join(marks) + ")")
    statement = f"INSERT INTO {table} ({', '.join(TXN_COLUMNS)}) VALUES " + ", ".join(value_rows)
    with _databricks(settings) as conn, conn.cursor() as cur:
        cur.execute(statement, params)


def _already_present(settings: Settings, plan: dict) -> list[str]:
    """Anything the plan would add that already exists, in any system. Empty means safe to apply."""
    found: list[str] = []
    ids = [row[0] for row in plan["transactions"]]
    marks = ", ".join(f":t{i}" for i in range(len(ids)))
    with _databricks(settings) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT transaction_id FROM {settings.databricks_transactions_table} WHERE transaction_id IN ({marks})",
            {f"t{i}": v for i, v in enumerate(ids)},
        )
        found += [f"Databricks {r[0]}" for r in cur.fetchall()]
    sf = _salesforce(settings)
    wanted = [p["Account_Id__c"] for p in plan["profiles"]]
    listed = ", ".join(f"'{a}'" for a in wanted)  # our own constants, never user input
    found += [f"Salesforce {r['Account_Id__c']}"
              for r in sf.query(f"SELECT Account_Id__c FROM Customer_Profile__c WHERE Account_Id__c IN ({listed})")["records"]]
    with _supabase(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT case_id FROM case_history WHERE case_id = ANY(%s)", ([c[0] for c in plan["cases"]],))
        found += [f"Supabase {r[0]}" for r in cur.fetchall()]
    return found


def apply(settings: Settings, plan: dict) -> None:
    clash = _already_present(settings, plan)
    if clash:
        sys.exit("Refusing to apply - these already exist: " + ", ".join(clash))

    _insert_transactions(settings, plan["transactions"])
    print(f"Databricks : added {len(plan['transactions'])} rows")

    sf = _salesforce(settings)
    for profile in plan["profiles"]:
        sf.Customer_Profile__c.create(profile)
    print(f"Salesforce : added {len(plan['profiles'])} profiles")

    with _supabase(settings) as conn, conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO case_history ({', '.join(CASE_COLUMNS)}) VALUES ({', '.join(['%s'] * len(CASE_COLUMNS))})",
            plan["cases"],
        )
    print(f"Supabase   : added {len(plan['cases'])} cases")


def remove(settings: Settings, plan: dict) -> None:
    ids = [row[0] for row in plan["transactions"]]
    marks = ", ".join(f":t{i}" for i in range(len(ids)))
    with _databricks(settings) as conn, conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {settings.databricks_transactions_table} WHERE transaction_id IN ({marks})",
            {f"t{i}": v for i, v in enumerate(ids)},
        )
    print(f"Databricks : removed rows {ids[0]} .. {ids[-1]}")

    sf = _salesforce(settings)
    listed = ", ".join(f"'{p['Account_Id__c']}'" for p in plan["profiles"])
    records = sf.query(f"SELECT Id FROM Customer_Profile__c WHERE Account_Id__c IN ({listed})")["records"]
    for record in records:
        sf.Customer_Profile__c.delete(record["Id"])
    print(f"Salesforce : removed {len(records)} profiles")

    with _supabase(settings) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM case_history WHERE case_id = ANY(%s)", ([c[0] for c in plan["cases"]],))
        print(f"Supabase   : removed {cur.rowcount} cases")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Add HIGH-risk demo accounts to the three source systems.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="add the records (default is a dry run)")
    mode.add_argument("--remove", action="store_true", help="delete exactly the records this script adds")
    args = parser.parse_args(argv[1:])

    plan = build_plan()
    settings = Settings()
    if args.apply:
        apply(settings, plan)
    elif args.remove:
        remove(settings, plan)
    else:
        print(describe(plan))
        print("\nDRY RUN - nothing was changed. Re-run with --apply to add these records.")


if __name__ == "__main__":
    main(sys.argv)
