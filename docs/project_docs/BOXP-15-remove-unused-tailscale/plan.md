# BOXP-15: Remove unused Tailscale configuration

## Goal

Remove the unused Tailscale Kubernetes Operator path from `lolice` and make the ArgoCD diff workflow use the existing Cloudflare path directly.

## Scope

- Remove the Tailscale Operator ArgoCD Application from `argoproj/kustomization.yaml`.
- Delete `argoproj/tailscale-operator`.
- Remove the ArgoCD Server `tailscale.com/expose` overlay from `argoproj/argocd/kustomization.yaml`.
- Delete `argoproj/argocd/overlays/argocd-server-tailscale.yaml`.
- Remove `tailscale/github-action` and `lolice-argocd` diagnostics from `.github/workflows/argocd-diff.yaml`.
- Keep historical project docs unchanged.

## Follow-up Outside Code

- Remove or retire Tailscale OAuth secrets and SSM parameters if they are no longer used:
  - `/lolice/tailscale/operator-oauth-client-id`
  - `/lolice/tailscale/operator-oauth-client-secret`
  - Tailscale GitHub Action secrets such as `TS_OAUTH_CLIENT_ID` and `TS_AUDIENCE`
- Close or supersede stale Tailscale-related PRs after replacement cleanup PRs are open.

## Verification

- Search for remaining active Tailscale references outside historical docs.
- Build relevant kustomize trees if local dependencies permit.
- Run YAML parsing against changed workflow/config files.
