# Ground Truth

Answer key for evaluating the incident-investigator agent on the OpenTelemetry demo shop.

| Flag | What the user sees | True failing service | What the trace shows | Evidence (error text or metric) |
|------|--------------------|----------------------|----------------------|---------------------------------|
| paymentFailure | checkout returns 422 | payment | deepest error span is `payment charge`, and no spans run after payment (no ship-order, EmptyCart or email) | TODO |
| cartFailure | TODO | TODO | TODO | TODO |
| productCatalogFailure | TODO | TODO | TODO | TODO |
| adFailure | TODO | TODO | TODO | TODO |
| paymentUnreachable | TODO | TODO | TODO | TODO |
| intlShippingSlowdown | TODO | TODO | TODO | TODO |

## How to read this

- The true failing service is the service of the deepest span with an error.
- Slowness flags (`intlShippingSlowdown`) are found by span duration, not errors.

http://localhost:8080/jaeger/ui/trace/fd5c913f412f6a32a82a8b501f425d33

http://localhost:8080/jaeger/ui/trace/a17e3a296c059ac4f6959d90831e0b63