WITH o AS (
  SELECT DISTINCT ON (model_version, target_id, session_date)
         model_version, target_id, session_date, forecast_run_id, probabilities, actual_label
    FROM forecast.prediction_outcomes
   WHERE target_id LIKE 'range%' AND eligible AND probabilities IS NOT NULL
     AND model_version IN ('nq_sklearn_v5', 'nq_climatology_v5')
   ORDER BY model_version, target_id, session_date, generated_at DESC, outcome_revision DESC
)
SELECT sk.target_id,
       r.calibration->'training'->sk.target_id->>'selected' AS selected,
       count(*) AS sessions, min(sk.session_date) AS first, max(sk.session_date) AS last,
       round(avg(-ln((sk.probabilities->>sk.actual_label)::float))::numeric, 4) AS logloss_model,
       round(avg(-ln((cl.probabilities->>cl.actual_label)::float))::numeric, 4) AS logloss_base,
       round(avg(ln((sk.probabilities->>sk.actual_label)::float)
                 - ln((cl.probabilities->>cl.actual_label)::float))::numeric, 4) AS gain
  FROM o sk
  JOIN o cl ON cl.model_version = 'nq_climatology_v5' AND cl.target_id = sk.target_id
           AND cl.session_date = sk.session_date
  JOIN forecast.forecast_runs r ON r.forecast_run_id = sk.forecast_run_id
 WHERE sk.model_version = 'nq_sklearn_v5'
 GROUP BY 1, 2
 ORDER BY 1, 4;