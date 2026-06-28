# B0XP-23 local LLM / GPU observability

## Goal

Add first-class observability for the GPU-backed local LLM workload using the repository's existing kube-prometheus GitOps pattern.

## Scope

- Add a Grafana dashboard for `local-llm` request/error/latency metrics and Intel GPU exporter metrics.
- Add Prometheus alerts for local LLM target health, local LLM 5xx ratio, and Intel GPU exporter health.
- Do not hand-edit image digests. `ghcr.io/boxp/arch/llama-sycl` stays managed by `argocd-image-updater`.

## Files

- `argoproj/prometheus-operator/local-llm-gpu-observability-rules.yaml`
- `argoproj/prometheus-operator/dashboards/local-llm-gpu-overview.json`
- `argoproj/prometheus-operator/kustomization.yaml`
- `argoproj/prometheus-operator/overlays/grafana.yaml`

## Validation

- `jq empty argoproj/prometheus-operator/dashboards/local-llm-gpu-overview.json`
- `kubectl kustomize argoproj/prometheus-operator`
- `kubectl apply --dry-run=server -f argoproj/prometheus-operator/local-llm-gpu-observability-rules.yaml`
- Prometheus API query checks for:
  - `up{job="llama-server", namespace="local-llm", service="llama-server"}`
  - `up{job="intel-gpu-exporter", namespace="intel-device-plugins", service="intel-gpu-exporter"}`
  - `envoy_cluster_upstream_rq_total`
  - `envoy_cluster_upstream_rq_time_bucket`

## Rollout

1. Merge the PR to `main`.
2. Let Argo CD sync `prometheus-operator`.
3. Verify `local-llm-gpu-observability-rules` exists in Prometheus.
4. Verify Grafana loads `Local LLM / GPU Overview`.
