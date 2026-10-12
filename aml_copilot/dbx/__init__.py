"""Running the copilot on Databricks: jobs that investigate, a notebook that reviews, Delta tables for state.

    runtime       secrets -> environment, and the Delta-backed job store / audit log / reviewer lookup
    investigate   one investigation = one job run (submit, run the pipeline, store the draft, write the audit trail)
    review        the human decision: list, show, approve / reject / escalate, with the caller's real identity
    jobs          the entry points the Databricks job tasks call (`investigate`, `canary`)

Nothing here imports `pyspark` or `dbutils` at module level, so everything is testable on a laptop.
"""
