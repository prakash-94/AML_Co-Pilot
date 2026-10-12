"""The service layer: what turns the investigation pipeline into a deployable, auditable API.

    models   what an API client sees (and, by construction, never the raw database rows)
    jobs     the job record and its rules (claim once, decide once, daily quota, no duplicates)
    audit    an append-only, hash-chained trail of everything that happened to a job
    auth     who is calling, and in which role
    api      the web endpoints a browser talks to        (role "api")
    worker   the endpoint that actually runs a job       (role "worker")

Nothing here imports torch or the embedding model; only the worker runs the pipeline.
"""
