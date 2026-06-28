# BOXP-23 Intel GPU metrics exporter

## Background

`golyat-4` exposes `gpu.intel.com/i915=1` and `gpu.intel.com/monitoring=1` through the Intel GPU device plugin. Phase 7 requires GPU usage observability before dashboards and alerts can be finalized.

## Plan

- Keep the exporter in the existing `intel-gpu-device-plugin` Argo CD Application.
- Use `gpu.intel.com/monitoring: 1` so the exporter receives monitoring access from the device plugin.
- Schedule only on `lolice.io/gpu-worker=true` / `intel.feature.node.kubernetes.io/gpu=true` nodes and tolerate the GPU worker taint.
- Expose `/metrics` through a Service and ServiceMonitor in `intel-device-plugins`.
- Pin the tested exporter image by digest instead of using the upstream `rolling` tag.
- Extend the existing Prometheus ServiceMonitor RBAC to include `discovery.k8s.io/endpointslices`, because Prometheus is configured with `serviceDiscoveryRole: EndpointSlice`.

## Validation

- `kubectl kustomize argoproj/intel-gpu-device-plugin`
- `kubectl apply --dry-run=server -k argoproj/intel-gpu-device-plugin`
- Argo CD `intel-gpu-device-plugin` reaches `Synced Healthy`
- `intel-gpu-exporter` DaemonSet is `1/1` on `golyat-4`
- `/metrics` returns `igpu_*` metrics such as `igpu_engines_render_3d_0_busy`, `igpu_power_gpu`, and `igpu_rc6`
- Prometheus target for `intel-gpu-exporter` is `up`
- Prometheus query `igpu_rc6` returns a sample for the exporter target

## Notes

An XPU Manager smoke test failed on `golyat-4` with `zeInit error: 78000001`, so the first production metrics path uses an `intel_gpu_top` based exporter. Revisit XPU Manager later if Intel Arc 140T support improves.
