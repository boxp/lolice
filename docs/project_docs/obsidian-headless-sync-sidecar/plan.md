# Obsidian Headless Sync Sidecar

## Goal

Run Obsidian Sync continuously inside the lolice Codex workspace Pod using the already installed `obsidian-headless` client.

## Plan

1. Add an `obsidian-sync` sidecar container to `argoproj/codex-workspace/deployment.yaml`.
2. Reuse `ghcr.io/boxp/arch/codex-workspace:latest`, which already includes `obsidian-headless` and exposes the `ob` command.
3. Mount the shared `/home/boxp` PVC into the sidecar so it can read the existing Obsidian headless config under `~/.config/obsidian-headless` and sync the local vault at `/home/boxp/Documents/obsidian-headless/BOXP`.
4. Run `ob sync --path "${OBSIDIAN_VAULT_PATH}" --continuous` as UID/GID 1000 with minimal privileges.
   Let Kubernetes restart the sidecar if the continuous sync process exits unexpectedly.
5. Validate the rendered Kustomize output.

## Notes

- The Docker image remains owned by `boxp/arch`; this change only controls runtime behavior in `boxp/lolice`.
- Obsidian account credentials and vault encryption state remain on the workspace PVC and are not committed to Git.
