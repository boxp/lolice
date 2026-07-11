# BOXP-100 Novel Board runner deployment plan

## Goal

Deploy the independent Novel Board runner shipped by `boxp/arch` alongside the existing Task Board runner without changing Task Board, Codex cron, or daily novel cron behavior.

## Changes

1. Add one `novel-board-runner` sidecar to `argoproj/codex-workspace/deployment.yaml`.
2. Run `/opt/codex-workspace/novel-board/novel_board_runner.bb loop` from the codex-workspace image.
3. Mount the shared home PVC so the runner can read the vault and persist private state under `/home/boxp/.novel-board`.
4. Use a separate root and `CODEX_NOVEL_BOARD_*` environment variables. Reuse only the Pod UID owner and shared credentials required by the installed agent CLIs.
5. Add a preStop hook that records planned shutdown for Novel Board locks.
6. Document rollout, diagnosis, and rollback ordering with the companion `boxp/arch` PR.

## Validation

- `kubectl kustomize argoproj/codex-workspace` renders successfully.
- The Argo CD kustomize image override resolves every codex-workspace container to the verified `sha-3759085` image built from the companion PR and containing `novel_board_runner.bb`.
- The rendered Deployment contains exactly one `novel-board-runner` container, private root, vault path, Pod UID owner, poll/stale values, resource limits, restricted security context, and home PVC mount.
- During a `Recreate` rollout with an active Novel lock, the planned-shutdown marker does not bypass the heartbeat guard: recovery occurs after the 180-second stale threshold (normally within 210 seconds with the 30-second poll), without launching duplicate work.
- Existing `task-board-runner`, `codex-cron-scheduler`, workspace, and daily novel configuration remain unchanged.

## Rollout order

The companion `boxp/arch` PR #11014 build published `sha-3759085` from its latest CI merge commit. This manifest pins that verified image in `.argocd-source-codex-workspace.yaml`, so merge only while the pin is present and the build-and-push check remains successful. A later image-updater commit may advance the tag only to another image that also contains `novel_board_runner.bb`.
