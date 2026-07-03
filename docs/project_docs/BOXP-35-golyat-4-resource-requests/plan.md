# BOXP-35 golyat-4 GPU workload resource requests

## Context

`golyat-4` は Intel GPU worker として使う。GPU workload は `local-llm` の `llama-server` と `stable-diffusion` の `stable-diffusion-webui` を切り替えて使い、同時稼働は前提にしない。目的は GPU workload を優先しつつ、GPU を要求しない通常 workload が余剰 CPU / memory に載る scheduler 上の余白を残すこと。

この task-board 実行環境には `kubectl` / `kustomize` が無かったため、クラスタ情報と実使用量は Grafana の Prometheus datasource proxy から確認した。Kubernetes API への直接アクセスは `https://10.96.0.1/version` が timeout した。

## Current manifests

### `local-llm` / `llama-server`

- manifest: `argoproj/local-llm/deployment.yaml`
- replicas: `1`
- strategy: `Recreate`
- priorityClassName: `gpu-workload-high`
- nodeSelector: `lolice.io/gpu-worker: "true"`
- toleration: `lolice.io/gpu-worker=true:NoSchedule`
- main container: `llama-server`
- sidecar: `envoy`
- GPU request / limit: `gpu.intel.com/i915: "1"`
- before:
  - `llama-server` requests: `cpu: "4"`, `memory: 16Gi`, `gpu.intel.com/i915: "1"`
  - `llama-server` limits: `gpu.intel.com/i915: "1"`
  - `envoy` requests: `cpu: 50m`, `memory: 64Mi`
  - `envoy` limits: `cpu: 500m`, `memory: 256Mi`

### `stable-diffusion` / `stable-diffusion-webui`

- manifest: `argoproj/stable-diffusion/deployment.yaml`
- replicas: `0`
- strategy: `Recreate`
- priorityClassName: `gpu-workload-high`
- nodeSelector: `lolice.io/gpu-worker: "true"`
- toleration: `lolice.io/gpu-worker=true:NoSchedule`
- main container: `sdnext`
- GPU request / limit: `gpu.intel.com/i915: "1"`
- before:
  - `sdnext` requests: `cpu: "4"`, `memory: 24Gi`, `gpu.intel.com/i915: "1"`
  - `sdnext` limits: `cpu: "14"`, `memory: 80Gi`, `gpu.intel.com/i915: "1"`

## `golyat-4` capacity and scheduler view

Prometheus query source: Grafana datasource proxy `P1809F7CD0C75ACF3` (`prometheus`).

Allocatable from `kube_node_status_allocatable{node="golyat-4"}`:

- CPU: `16` cores
- memory: `100631683072` bytes, about `93.72Gi`
- `gpu.intel.com/i915`: `1`
- `gpu.intel.com/monitoring`: `1`
- pods: `110`

Capacity from `kube_node_status_capacity{node="golyat-4"}`:

- CPU: `16` cores
- memory: `100736540672` bytes, about `93.82Gi`
- `gpu.intel.com/i915`: `1`
- `gpu.intel.com/monitoring`: `1`
- pods: `110`

Taint from `kube_node_spec_taint{node="golyat-4"}`:

- `lolice.io/gpu-worker=true:NoSchedule`

Current scheduled pods from `kube_pod_info{node="golyat-4"}` include:

- `local-llm/llama-server-64fc6cf47f-79bhd`
- `intel-device-plugins/intel-gpu-plugin-j7sxk`
- `intel-device-plugins/intel-gpu-exporter-d5kzf`
- node/system/monitoring/longhorn pods

Current allocated requests from `sum by (resource, unit) (kube_pod_container_resource_requests{node="golyat-4"})` before this manifest change:

- CPU: `6.535` cores
- memory: `18366857216` bytes, about `17.11Gi`
- `gpu.intel.com/i915`: `1`
- `gpu.intel.com/monitoring`: `1`

Current allocated limits from `sum by (resource, unit) (kube_pod_container_resource_limits{node="golyat-4"})` before this manifest change:

- CPU: `1.91` cores, excluding unlimited containers
- memory: `2415919104` bytes, about `2.25Gi`, excluding unlimited containers
- `gpu.intel.com/i915`: `1`
- `gpu.intel.com/monitoring`: `1`

## Observed usage

Prometheus query window: 7 days, using:

- memory: `max by (namespace, container) (max_over_time(container_memory_working_set_bytes{namespace=~"local-llm|stable-diffusion",container!="",container!="POD",pod!=""}[7d]))`
- CPU: `max by (namespace, container) (max_over_time((rate(container_cpu_usage_seconds_total{namespace=~"local-llm|stable-diffusion",container!="",container!="POD",pod!=""}[5m]))[7d:1m]))`

Peak memory:

- `local-llm/llama-server`: `25.22Gi`
- `local-llm/envoy`: `0.07Gi`
- `stable-diffusion/sdnext`: `22.12Gi`
- `stable-diffusion/cloudflared`: `0.03Gi`

Peak CPU:

- `local-llm/llama-server`: `14.75` cores
- `local-llm/envoy`: `0.01` cores
- `stable-diffusion/sdnext`: `2.27` cores
- `stable-diffusion/cloudflared`: `0.01` cores

Current `local-llm` smoke checks during this run:

- `GET http://llama-server.local-llm.svc.cluster.local:8080/health` returned `{"status":"ok"}`.
- `GET /v1/models` returned model `default`; initially `status.value` was `unloaded`.
- Short chat completion request with `max_tokens: 8` timed out after 180 seconds and left model status as `loading`. This is recorded as a pre-change runtime issue, not caused by the manifest change in this branch.

`stable-diffusion-webui` is currently `replicas: 0`, so this run did not start it or run image generation. Historical Prometheus data includes previous `sdnext` pods with memory and CPU peaks above.

## Chosen values

### Priority

- Add `argoproj/gpu-workload-priority/priorityclass.yaml`.
- PriorityClass name: `gpu-workload-high`
- Priority value: `100000`
- `preemptionPolicy: PreemptLowerPriority`
- Apply `priorityClassName: gpu-workload-high` to `local-llm` and `stable-diffusion-webui`.

Rationale:

- GPU workloads should win over opportunistic non-GPU workloads on `golyat-4`.
- Scheduler preemption is request-based. If a GPU workload is Pending because normal workloads consumed the CPU / memory request room, the higher priority allows Kubernetes to preempt lower-priority pods.
- Runtime CPU burst alone does not make Kubernetes immediately evict other running pods. During node pressure, kubelet eviction can use pod priority and request usage, so the GPU workloads should have both a high priority and realistic memory requests.
- The value is intentionally below Kubernetes system critical priorities while still above the default priority `0` used by ordinary workloads.

### `local-llm` / `llama-server`

- after requests: `cpu: "4"`, `memory: 28Gi`, `gpu.intel.com/i915: "1"`
- after limits: `gpu.intel.com/i915: "1"`

Rationale:

- 7 day memory peak was `25.22Gi`; the old `16Gi` request was below observed loaded-model usage.
- `28Gi` leaves about `2.78Gi` over the observed peak while keeping more than `60Gi` allocatable memory for node/system pods and normal non-GPU workloads.
- CPU request stays at `4` because local-llm has observed short 5 minute CPU peaks near `14.75` cores. The request should not be reduced while GPU workload priority is required; no CPU limit is added so inference can burst.

### `stable-diffusion` / `stable-diffusion-webui`

- after requests: `cpu: "3"`, `memory: 24Gi`, `gpu.intel.com/i915: "1"`
- after limits: `cpu: "14"`, `memory: 48Gi`, `gpu.intel.com/i915: "1"`

Rationale:

- 7 day memory peak was `22.12Gi`; `24Gi` keeps a small buffer without reserving the 48Gi upper candidate.
- 48Gi is kept as the memory limit rather than the request. That protects the node from an 80Gi runaway while still allowing a much larger burst than the observed peak.
- 7 day CPU peak was `2.27` cores, so request is reduced from `4` to `3`. CPU limit remains `14` so generation can burst if a heavier model or extension needs it.
- `replicas: 0` remains unchanged because current operation switches between local-llm and stable-diffusion instead of running both GPU workloads at once.

## Expected scheduler impact

When `local-llm` is running, memory request increases by `12Gi` compared with the old manifest because the old request was below measured usage. With `golyat-4` allocatable memory at about `93.72Gi`, this still leaves roughly `60Gi` for non-GPU workloads after existing non-GPU node overhead.

When `stable-diffusion-webui` is running instead of `local-llm`, CPU request decreases by `1` core and memory request stays `24Gi`; memory limit decreases from `80Gi` to `48Gi`. This keeps normal workload scheduling room while avoiding a high memory limit that could consume almost the entire node.

## Non-GPU workload placement

`golyat-4` currently has `lolice.io/gpu-worker=true:NoSchedule`. That taint is the broad blocker for normal non-GPU workloads, even after CPU / memory request room exists.

This PR intentionally does not add `lolice.io/gpu-worker` tolerations to many non-GPU workloads. Adding tolerations across unrelated manifests would make the GitOps change larger than needed and would preserve the dedicated-node taint model. The desired operating model after this PR is:

- keep GPU workloads pinned to `golyat-4` with `nodeSelector: lolice.io/gpu-worker=true` and `gpu.intel.com/i915: "1"`;
- size GPU workload CPU / memory requests so they reserve enough room for stable startup and inference/generation;
- give GPU workloads `priorityClassName: gpu-workload-high`, so they can preempt lower-priority normal workloads if they otherwise cannot be scheduled;
- after the PR is merged and synced, remove the node taint from `golyat-4` so ordinary non-GPU workloads can use spare CPU / memory without per-workload toleration changes.

Post-merge taint removal command:

```bash
kubectl taint node golyat-4 lolice.io/gpu-worker=true:NoSchedule-
```

Rollback command if `golyat-4` should become dedicated again:

```bash
kubectl taint node golyat-4 lolice.io/gpu-worker=true:NoSchedule
```

## Validation

Local render validation in this run:

- `kubectl` was unavailable.
- `kustomize` was unavailable.
- `go` was unavailable, so `go run sigs.k8s.io/kustomize/kustomize/v5` was not an option.
- `npx --yes kustomize build argoproj/local-llm` succeeded.
- `npx --yes kustomize build argoproj/stable-diffusion` succeeded.
- `npx --yes kustomize build argoproj/gpu-workload-priority` succeeded.

Follow-up validation from a local environment with `kubectl`:

- `kubectl kustomize argoproj/local-llm` succeeded.
- `kubectl kustomize argoproj/stable-diffusion` succeeded.
- `kubectl kustomize argoproj/gpu-workload-priority` succeeded.
- `kubectl apply --dry-run=server -k argoproj/gpu-workload-priority` succeeded.
- `kubectl apply --dry-run=server -k argoproj/local-llm` and `kubectl apply --dry-run=server -k argoproj/stable-diffusion` timed out at the API server during this follow-up; local render succeeded for both.

The following must be run from an environment with `kubectl` after review/merge:

```bash
kubectl kustomize argoproj/gpu-workload-priority
kubectl kustomize argoproj/local-llm
kubectl kustomize argoproj/stable-diffusion
```

Post-sync smoke checks:

```bash
kubectl -n local-llm rollout status deploy/llama-server
kubectl -n local-llm get pod -o wide
kubectl -n local-llm port-forward svc/llama-server 8080:8080
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"default","messages":[{"role":"user","content":"Say OK in one word."}],"max_tokens":8,"temperature":0}'

kubectl -n stable-diffusion scale deploy/stable-diffusion-webui --replicas=1
kubectl -n stable-diffusion rollout status deploy/stable-diffusion-webui
kubectl -n stable-diffusion get pod -o wide
kubectl -n stable-diffusion port-forward svc/stable-diffusion-webui 7860:7860
curl -fsS http://127.0.0.1:7860/
kubectl -n stable-diffusion scale deploy/stable-diffusion-webui --replicas=0

kubectl describe node golyat-4
```

Confirm that `kubectl describe node golyat-4` shows the expected request room for CPU and memory, especially in the active operating mode:

- local-llm mode: `local-llm` consumes one Intel GPU and requests `4` CPU / `28Gi` memory.
- stable-diffusion mode: `stable-diffusion-webui` consumes one Intel GPU and requests `3` CPU / `24Gi` memory.
- both GPU workloads use `priorityClassName: gpu-workload-high`, so they can preempt lower-priority pods when scheduler request room is unavailable.
- after `lolice.io/gpu-worker=true:NoSchedule` is removed from `golyat-4`, normal workloads can be scheduled there if the scheduler chooses it and they do not request the Intel GPU resource.

## Rollback

Revert these resource values in GitOps and sync Argo CD:

- `argoproj/gpu-workload-priority/`
  - remove the `gpu-workload-high` `PriorityClass` app if GPU workload priority should be disabled
- `argoproj/local-llm/deployment.yaml`
  - `llama-server.resources.requests.memory`: `16Gi`
  - remove `priorityClassName: gpu-workload-high`
- `argoproj/stable-diffusion/deployment.yaml`
  - `sdnext.resources.requests.cpu`: `"4"`
  - `sdnext.resources.limits.memory`: `80Gi`
  - remove `priorityClassName: gpu-workload-high`

If normal workloads should again avoid `golyat-4`, restore the node taint:

```bash
kubectl taint node golyat-4 lolice.io/gpu-worker=true:NoSchedule
```
  - `argoproj/argocd/cloudflared-deployment.yaml`
  - `argoproj/argocd/base/cloudflared-api.yaml`
  - `argoproj/minecraft/base/cloudflared-deployment.yaml`

Rollback commands after reverting the manifest:

```bash
kubectl -n local-llm rollout restart deploy/llama-server
kubectl -n local-llm rollout status deploy/llama-server

kubectl -n stable-diffusion rollout restart deploy/stable-diffusion-webui
kubectl -n stable-diffusion rollout status deploy/stable-diffusion-webui
```
