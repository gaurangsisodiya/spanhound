SELECT ServiceName, SpanName, SpanKind, StatusMessage, count() AS n
FROM otel_traces
WHERE StatusCode = 'Error'
  AND Timestamp >= {start:DateTime} AND Timestamp < {end:DateTime}
  AND SpanName NOT LIKE '%EventStream%'
  -- no error span below it: nothing deeper failed
  AND (TraceId, SpanId) NOT IN (
      SELECT TraceId, ParentSpanId FROM otel_traces
      WHERE StatusCode = 'Error'
        AND Timestamp >= {start:DateTime} - INTERVAL 5 MINUTE
        AND Timestamp <  {end:DateTime} + INTERVAL 5 MINUTE
  )
  -- a Client span whose callee answered is passing an error along, not causing it
  AND NOT (SpanKind = 'Client' AND (TraceId, SpanId) IN (
      SELECT TraceId, ParentSpanId FROM otel_traces
      WHERE Timestamp >= {start:DateTime} - INTERVAL 5 MINUTE
        AND Timestamp <  {end:DateTime} + INTERVAL 5 MINUTE
  ))
GROUP BY ServiceName, SpanName, SpanKind, StatusMessage
ORDER BY n DESC, (StatusMessage != '') DESC, ServiceName
