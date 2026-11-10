# Decisions

- Exclude flagd EventStream spans from latency and error queries: they are 10-minute streaming connections that end with a timeout error every cycle, not requests.
- Keep the 15:39-15:44 UTC product-catalog starvation as an unplanned incident in the eval notes (all flags off). Culprit: product-catalog, not recommendation.
