# BOXP-57 local-llm EVO-T1 tuning

## Goal

Make the `local-llm` Gemma4 runtime safer on EVO-T1 / Arrow Lake iGPU by avoiding the known-risk SYCL combination of Flash Attention forced on with quantized KV cache, then benchmark MoE offload and thread settings before any further performance tuning.

## Changes

1. Remove explicit `--cache-type-k q8_0` and `--cache-type-v q8_0` from `argoproj/local-llm/deployment.yaml`.
   - llama.cpp defaults the KV cache back to f16.
   - This avoids the quantized-KV path reported to segfault or corrupt output on SYCL / Arrow Lake iGPU.
2. Change `-fa on` to `-fa auto`.
   - This lets llama.cpp choose a compatible attention path instead of forcing the problematic path.
3. Remove `--cpu-moe`.
   - EVO-T1's iGPU shares system memory bandwidth with the CPU, so CPU MoE offload is not expected to reduce memory pressure in the same way it does on a discrete GPU.
   - The final decision should still be confirmed with `llama-bench` on the GPU worker.

## Validation

Run manifest validation:

```bash
kubectl kustomize argoproj/local-llm >/tmp/lolice-local-llm.yaml
kubectl apply --dry-run=server -k argoproj/local-llm
```

After Argo CD sync:

```bash
kubectl -n argocd get app local-llm -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
kubectl -n local-llm rollout status deploy/llama-server
kubectl -n local-llm logs deploy/llama-server -c llama-server --tail=200
curl -sS http://llama-server.local-llm.svc.cluster.local:8080/health
curl -sS http://llama-server.local-llm.svc.cluster.local:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer local-llm' \
  -d '{"model":"gemma4-26b","messages":[{"role":"user","content":"日本語で、文字化けせずに一文で挨拶して。"}],"max_tokens":64,"temperature":0}'
```

## Benchmark Plan

The current `ghcr.io/boxp/arch/llama-sycl` image contains `llama-server` but not `llama-bench`, so benchmark execution needs either a bench-capable image or a temporary debug image on `golyat-4`.

Baseline matrix:

```bash
llama-bench \
  -m /models/Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf \
  -ngl 99 \
  -p 512 \
  -n 128 \
  -t 8,12,14 \
  -fa 0,1
```

MoE matrix:

```bash
llama-bench \
  -m /models/Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf \
  -ngl 99 \
  -p 512 \
  -n 128 \
  -t 14 \
  --n-cpu-moe 0,16,999
```

Record pp512 and tg128 results here after running the matrix.

## Notes

- 2026-07-07: Live `local-llm` Deployment was running `--cache-type-k q8_0`, `--cache-type-v q8_0`, `-fa on`, and `--cpu-moe`.
- 2026-07-07: The live container has `/opt/llama.cpp/bin/llama-server` but no `llama-bench`, so repository changes were prepared first and benchmark execution remains a follow-up deployment validation step.
