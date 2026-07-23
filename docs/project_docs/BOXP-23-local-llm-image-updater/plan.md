# Plan: local-llm ImageUpdater

## Context

`local-llm` consumes `ghcr.io/boxp/arch/llama-sycl:latest`. The repository convention is to let Argo CD Image Updater write the resolved image update back to `boxp/lolice` instead of manually restarting workloads to pick up `latest`.

## Change

- Add an `ImageUpdater` for the `local-llm` Application.
- Track `ghcr.io/boxp/arch/llama-sycl:latest` with `digest` for `linux/amd64`.
- Use the existing `git:secret:argocd/repo-lolice` write-back method targeting `main`.
- Keep the generated `.argocd-source-local-llm.yaml` on the current `latest` digest. `newest-build` selected the older `sha-87fdeb8` tag from GHCR, so digest tracking is required for this `latest`-driven image.

## Verification

- Render `argoproj/argocd-image-updater` with `kubectl kustomize`.
- Server dry-run the rendered manifests.
- Sync `argocd-image-updater`, then let Image Updater update `local-llm` image state.
