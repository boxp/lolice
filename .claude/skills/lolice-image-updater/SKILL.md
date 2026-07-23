---
name: lolice-image-updater
description: Use when changing container images in boxp/lolice Kubernetes manifests, adding a new workload image, or discussing image rollout ownership; enforces Argo CD Image Updater ownership instead of manual tag or digest edits.
---

# lolice Image Updater

For `boxp/lolice`, do not hand-edit workload image tags or digests as the rollout mechanism. Container image updates are owned by Argo CD Image Updater whenever the image is built outside this repo or should continuously follow a registry tag.

## Workflow

1. Check existing ImageUpdater patterns:
   - `argoproj/argocd-image-updater/imageupdaters/*.yaml`
   - `argoproj/argocd-image-updater/imageupdaters/kustomization.yaml`
2. Keep the workload manifest image at the logical registry/tag, usually `ghcr.io/boxp/arch/<image>:latest` or the ECR `...amazonaws.com/<image>:latest`.
3. Add or update an `ImageUpdater` resource for the Argo CD Application.
4. Use `writeBackConfig.method: git:secret:argocd/repo-lolice` and target `branch: main`.
5. Choose registry-specific settings:
   - GHCR `boxp/arch` images: follow `local-llm.yaml`; use `updateStrategy: digest`, set `platforms: ["linux/amd64"]`, and set `gitConfig.repository: git@github.com:boxp/lolice.git`.
   - ECR images: follow `ark-discord-bot.yaml` / `palserver.yaml`; use `updateStrategy: newest-build` and `pullSecret: pullsecret:argocd/regcred`.
6. Set `manifestTargets.kustomize.name` to the image name without tag.
7. Validate with:
   - `kubectl kustomize argoproj/argocd-image-updater`
   - `kubectl kustomize argoproj/<application>`

## Guardrails

- Do not pin generated `sha-*` tags manually in workload manifests.
- Do not commit Image Updater write-back output unless the task is specifically to inspect or repair it.
- If changing runtime args/resources for a workload, make that manifest change separately from image rollout ownership.
