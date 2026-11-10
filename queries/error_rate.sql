SELECT
    ServiceName,
    count() AS total_spans,
    countIf(StatusCode = 'Error') AS error_spans,
    round(error_spans * 100.0 / total_spans, 2) AS error_pct
FROM otel.otel_traces
WHERE Timestamp >= {start:DateTime}
  AND Timestamp <  {end:DateTime}
  AND SpanName NOT LIKE '%EventStream%'
GROUP BY ServiceName
ORDER BY error_pct DESC
