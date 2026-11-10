# Decisions

- Exclude flagd EventStream spans from latency and error queries: they are 10-minute streaming connections that end with a timeout error every cycle, not requests.
- Keep the 15:39-15:44 UTC product-catalog starvation as an unplanned incident in the eval notes (all flags off). Culprit: product-catalog, not recommendation.
- error_origins drops Client error spans whose callee responded (HTTP 4xx marks only the client span as an error, so the error chain breaks at each HTTP hop).
- Timeout and cancellation errors point at whoever gave up, not the cause; slowness incidents need a latency tool.
- Latency ranks by p95 self time (duration minus children), not total time, so services that only wait on others don't rank high.
- Latency excludes frontend-web (browser telemetry: network and client time, not a backend cause) and the proxy's flag routes (long-lived connections, slow in every window).
- error_origins is the primary error tool; error_rate can rank callers above the culprit because services emit different numbers of healthy spans per request (adFailure: ad 5.7%, frontend-proxy 11.4%).
- Error text may be in Events (exception.message), not StatusMessage; evidence summaries must read both.
