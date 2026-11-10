SELECT
    ServiceName,
    replaceRegexpAll(substring(Body, 1, 200), '[0-9a-f]{8,}|\\d+', '<N>') AS pattern,
    count() AS n,
    min(Timestamp) AS first_seen,
    anyIf(TraceId, TraceId != '') AS example_trace
FROM otel_logs
WHERE Timestamp >= {start:DateTime} AND Timestamp < {end:DateTime}
  AND SeverityNumber >= 13
  AND ServiceName != 'otelcol-contrib'
GROUP BY ServiceName, pattern
ORDER BY n DESC
LIMIT 20
