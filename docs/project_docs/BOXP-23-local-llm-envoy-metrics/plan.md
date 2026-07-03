# BOXP-23 local-llm Envoy metrics

## Background

The current TurboQuant `llama-server` build does not expose native Prometheus metrics on `/metrics`. Phase 7 still needs request, error, and latency observability for `local-llm`.

## Plan

- Keep the Kubernetes Service endpoint as `llama-server.local-llm.svc.cluster.local:8080`.
- Move `llama-server` inside the Pod to `0.0.0.0:8081`. The port is only exposed as a container port for kubelet probes and is not published by the Service.
- Add an Envoy sidecar listening on `0.0.0.0:8080` and proxying to `127.0.0.1:8081`.
- Expose Envoy admin `/stats/prometheus` on port `9901` through the same Service.
- Add a ServiceMonitor for Envoy stats.
- Pin the Envoy image by digest.

## Validation

- `kubectl kustomize argoproj/local-llm`
- `kubectl apply --dry-run=server -k argoproj/local-llm`
- `local-llm` Argo CD Application reaches `Synced Healthy`
- `/health`, `/v1/models`, and `/v1/chat/completions` still work through Service port `8080`
- `/stats/prometheus` returns Envoy metrics
- Prometheus target for `llama-server-envoy` is `up`
- Prometheus query returns `envoy_cluster_upstream_rq_time` and request/error counters for `llama_server`
