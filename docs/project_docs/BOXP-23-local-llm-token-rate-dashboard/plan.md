# BOXP-23 local LLM token rate dashboard

## Goal

Show local LLM token throughput on the existing `Local LLM / GPU Overview` dashboard.

## Background

The current `llama-server` / Envoy / Prometheus path does not expose native token throughput metrics. The available token/s signal is emitted in `llama-server` logs, for example `tokens per second` timing lines and TurboQuant/SYCL slot timing lines such as `tg = ... t/s`. Loki is configured as a Grafana datasource, so the first implementation should add LogQL panels that extract token throughput from `local-llm` logs.

## Scope

- Add token/s panels to `argoproj/prometheus-operator/dashboards/local-llm-gpu-overview.json`.
- Use the existing Loki datasource by name for LogQL panels.
- Support both classic llama timing logs and the live TurboQuant/SYCL `tg = ... t/s` generation timing format.
- Keep existing Prometheus request, latency, GPU, and temperature panels unchanged.
- Fix the `argocd-diff` workflow kustomize install step if the PR check fails before manifest validation.
- Do not edit `local-llm` workload image tags or digests.

## Validation

- `jq empty argoproj/prometheus-operator/dashboards/local-llm-gpu-overview.json`
- `kubectl kustomize argoproj/prometheus-operator`
- YAML parse of `.github/workflows/argocd-diff.yaml`
- Local `/tmp` smoke of the pinned kustomize v5.7.1 download/extract path used by `argocd-diff`
- Live Loki query check for `local-llm` `llama-server` `tokens per second` log lines.
- Live Loki query check for `local-llm` `llama-server` `tg = ... t/s` slot timing log lines.
- Confirm whether the current `llama-server` Pod stream is present in Loki; if not, track Alloy/Loki ingestion separately.

## Rollout

1. Merge the PR to `main`.
2. Let Argo CD sync `prometheus-operator`.
3. Verify Grafana reloads `Local LLM / GPU Overview`.
4. Generate a short `local-llm` request and confirm token/s panels update from Loki.
