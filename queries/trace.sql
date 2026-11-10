SELECT
    SpanId,
    ParentSpanId,
    ServiceName,
    SpanName,
    SpanKind,
    round(Duration / 1e6, 1) AS ms,
    StatusCode,
    StatusMessage,
    arrayStringConcat(
        arrayFilter(m -> m != '', arrayMap(a -> a['exception.message'], `Events.Attributes`)),
        ' | ') AS exceptions
FROM otel_traces
WHERE TraceId = {trace_id:String}
ORDER BY Timestamp
LIMIT 200
