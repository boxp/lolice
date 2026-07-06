# ImageUpdater Scope

`argoproj/argocd-image-updater/imageupdaters/` is for images that are built and published by this project or its owned infrastructure.

Use Argo CD Image Updater for images such as:

- ECR images produced by this project
- `ghcr.io/boxp/arch/*` images built from the owned image repository

Do not add third-party runtime images here. Images such as `docker.io/library/node`, `docker.io/cloudflare/cloudflared`, and `docker.io/nousresearch/hermes-agent` should stay pinned by digest in the workload manifests and be updated by Renovate. Prefer `tag@sha256` where the registry supports it; use `repository@sha256` for images whose tagged digest reference is not pullable. This keeps ImageUpdater write-back limited to images whose publication cadence is controlled by this project, and avoids Argo CD replacing reviewed third-party image tags or digests during sync.
