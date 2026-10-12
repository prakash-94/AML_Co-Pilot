"""The service rules that make an investigation trustworthy, independent of where they run.

    models     what is stored about an investigation (and, by construction, never the raw database rows)
    jobs       the job record and its rules (run once, decide once, daily limit, no duplicates)
    audit      an append-only, hash-chained trail of everything that happened to a job
    auth       the errors raised when a role may not do something
    worker     `process_job`: claim, run the pipeline, store the draft, write the audit events
    pipeline   how the real pipeline is wired and what provenance is recorded
    delta      the Delta-table storage used on Databricks

Nothing here imports torch or the embedding model; only `pipeline.make_run_fn` loads the agents, and only when called.
"""
