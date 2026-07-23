# B0XP-23 GPU worker temperature observability

## Goal

Add the available thermal signal for the GPU worker to the local LLM / GPU observability dashboard and alerts.

## Context

`golyat-4` uses Intel Arc 140T iGPU. The current `intel_gpu_top` exporter provides engine busy, frequency, memory bandwidth, power, and RC6, but not a discrete GPU temperature or VRAM metric.

Current host evidence:

- no GPU-specific DRM hwmon directory under `/sys/class/drm/card0/device/hwmon`
- `intel_gpu_top -J` does not expose temperature or VRAM
- `node_thermal_zone_temp{instance="golyat-4", type="x86_pkg_temp"}` is available in Prometheus
- `clinfo` reports OpenCL global memory as shared system memory, not discrete VRAM

## Change

- Add `GPU Worker Package Temperature` panel to `Local LLM / GPU Overview`.
- Add `GpuWorkerPackageTemperatureHigh` alert using `golyat-4` `x86_pkg_temp`.

## Validation

- `jq empty argoproj/prometheus-operator/dashboards/local-llm-gpu-overview.json`
- `kubectl kustomize argoproj/prometheus-operator`
- `kubectl --server=https://192.168.10.102:6443 apply --dry-run=server -f argoproj/prometheus-operator/local-llm-gpu-observability-rules.yaml`
- Prometheus API query:
  - `node_thermal_zone_temp{job="node-exporter", instance="golyat-4", type="x86_pkg_temp"}`
