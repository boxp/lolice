# BOXP-33: Codex Task Board runner sidecar

## Goal

Run the Codex Task Board runner inside the codex-workspace Pod so Obsidian Task Board tickets assigned to Codex are processed automatically.

## Plan

1. Add a `task-board-runner` sidecar to `argoproj/codex-workspace/deployment.yaml`.
2. Use the codex-workspace image and run `/opt/codex-workspace/task-board/task_board_runner.bb loop`.
3. Mount the shared home PVC so the runner can read the Obsidian vault and persist `/home/boxp/.codex-task-board` state.
4. Pass Codex, Grafana, Gemini, and Docker-related environment consistent with the existing workspace / cron containers.
5. Mount the Docker CLI into the sidecar for tasks that need Docker during Codex execution.

## Validation

- `kubectl kustomize argoproj/codex-workspace`.
- `git diff --check`.
