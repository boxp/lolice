# BOXP-23 local-llm no-mmap

## Context

Production official SYCL `llama-server` works with `--cpu-moe`, but logs warn:

```text
tensor overrides to CPU are used with mmap enabled - consider using --no-mmap for better performance
```

The current production baseline is:

- Long prompt eval: `29553 tokens / 28.18 tok/s`
- Long generation: `335 tokens / 4.16 tok/s`
- Short smoke generation: `8.92 tok/s`

## Plan

1. Add `--no-mmap` to the `local-llm` router args while keeping the current official SYCL settings.
2. Render and server-side dry-run the `argoproj/local-llm` manifests.
3. Merge through GitOps, wait for Argo CD to sync, and verify the running Pod args include `--no-mmap`.
4. Reload Gemma4 and confirm the mmap warning disappears or changes.
5. Run a Japanese smoke request and compare prompt/generation token rate against the current production baseline.

## Validation

- `kubectl kustomize argoproj/local-llm`
- `kubectl apply --dry-run=server -k argoproj/local-llm`
- Argo CD `local-llm` reaches `Synced Healthy`
- Production `/v1/chat/completions` returns readable Japanese
