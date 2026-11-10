WITH children AS (
    SELECT TraceId, ParentSpanId AS parent_id, sum(Duration) AS child_ns
    FROM otel.otel_traces
    WHERE Timestamp >= {start:DateTime} - INTERVAL 5 MINUTE
      AND Timestamp <  {end:DateTime} + INTERVAL 5 MINUTE
    GROUP BY TraceId, ParentSpanId
)
SELECT
    s.ServiceName,
    s.SpanName,
    s.SpanKind,
    count() AS spans,
    round(quantile(0.95)(s.Duration) / 1e6, 1) AS p95_total_ms,
    round(quantile(0.95)(greatest(toInt64(s.Duration) - toInt64(c.child_ns), 0)) / 1e6, 1) AS p95_self_ms
FROM otel.otel_traces AS s
LEFT JOIN children AS c ON s.TraceId = c.TraceId AND s.SpanId = c.parent_id
WHERE s.Timestamp >= {start:DateTime} AND s.Timestamp < {end:DateTime}
  AND s.SpanName NOT LIKE '%EventStream%'
  AND s.ServiceName != 'frontend-web'
  AND s.SpanName NOT LIKE 'router flag%'
GROUP BY s.ServiceName, s.SpanName, s.SpanKind
ORDER BY p95_self_ms DESC
LIMIT 15
