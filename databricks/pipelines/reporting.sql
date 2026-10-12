-- Lakeflow Spark Declarative Pipeline: turns the service's raw Delta tables into tables people can chart.
-- Runs on serverless compute; every object below is a materialized view in workspace.aml_copilot.

-- One row per investigation, read out of the JSON documents the service stores.
CREATE OR REFRESH MATERIALIZED VIEW investigations_flat
COMMENT 'One row per investigation (from the service documents)'
AS SELECT
  get_json_object(body, '$.job_id')                                   AS job_id,
  get_json_object(body, '$.account_id')                               AS account_id,
  get_json_object(body, '$.status')                                   AS job_status,
  get_json_object(body, '$.review_status')                            AS review_status,
  to_timestamp(get_json_object(body, '$.created_at'))                 AS created_at,
  to_timestamp(get_json_object(body, '$.finished_at'))                AS finished_at,
  get_json_object(body, '$.result.status')                            AS outcome,
  get_json_object(body, '$.result.risk_score.tier')                   AS risk_tier,
  CAST(get_json_object(body, '$.result.risk_score.score') AS INT)     AS risk_score,
  CAST(get_json_object(body, '$.result.provenance.step_seconds.total') AS DOUBLE) AS seconds,
  get_json_object(body, '$.result.provenance.llm_provider')           AS llm_provider,
  get_json_object(body, '$.decision.decision')                        AS decision,
  get_json_object(body, '$.decision.role')                            AS decided_by_role,
  get_json_object(body, '$.error')                                    AS error
FROM workspace.aml_copilot.documents
WHERE collection = 'jobs';

CREATE OR REFRESH MATERIALIZED VIEW investigations_daily
COMMENT 'Investigations per day by outcome, risk tier and review state'
AS SELECT
  date(created_at) AS day,
  outcome, risk_tier, review_status,
  count(*)                    AS investigations,
  round(avg(seconds), 1)      AS avg_seconds
FROM investigations_flat
GROUP BY date(created_at), outcome, risk_tier, review_status;

CREATE OR REFRESH MATERIALIZED VIEW canary_trend
COMMENT 'Quality of each scheduled canary run: tier accuracy, supported-sentence rate, cost'
AS SELECT
  run_id,
  min(run_at)                                                   AS run_at,
  any_value(git_sha)                                            AS git_sha,
  any_value(llm_provider)                                       AS llm_provider,
  count(*)                                                      AS accounts,
  round(avg(CASE WHEN tier_ok THEN 1.0 ELSE 0.0 END), 3)        AS tier_accuracy,
  sum(supported)                                                AS sentences_supported,
  sum(sentences)                                                AS sentences_total,
  round(try_divide(sum(supported), sum(sentences)), 3)          AS supported_rate,
  round(sum(cost_usd), 3)                                       AS cost_usd,
  round(sum(seconds), 0)                                        AS seconds
FROM workspace.aml_copilot.eval_runs
GROUP BY run_id;

-- Checks the audit trail's hash chain in SQL: every event must name the hash of the event before it.
CREATE OR REFRESH MATERIALIZED VIEW audit_chain_links
COMMENT 'Audit events with a link check: does prev_hash equal the previous event hash?'
AS SELECT
  job_id, seq, event_type, actor, at, event_hash,
  prev_hash = coalesce(lag(event_hash) OVER (PARTITION BY job_id ORDER BY seq),
                       '0000000000000000000000000000000000000000000000000000000000000000') AS link_ok,
  seq = row_number() OVER (PARTITION BY job_id ORDER BY seq) AS sequence_ok
FROM (
  SELECT
    get_json_object(data, '$.job_id')     AS job_id,
    CAST(get_json_object(data, '$.seq') AS INT) AS seq,
    get_json_object(data, '$.event_type') AS event_type,
    get_json_object(data, '$.actor')      AS actor,
    get_json_object(data, '$.at')         AS at,
    get_json_object(data, '$.prev_hash')  AS prev_hash,
    get_json_object(data, '$.event_hash') AS event_hash
  FROM workspace.aml_copilot.audit_objects
);
