# BOXP-252: codex-workspace Pod 終了時の graceful shutdown（lolice 側）

設計正本は Obsidian `Projects/codex-task-board-runner/graceful-shutdown/design.md`。companion: boxp/arch feature/BOXP-252-graceful-shutdown。

## 変更

- `task-board-runner` container の command を `tini -- task_board_runner.bb loop` にする（bb を PID 1 にしない。孤児 zombie の reap と SIGTERM 転送）。tini は image に同梱済み（`/usr/bin/tini`）。
- `terminationGracePeriodSeconds: 60` と preStop `prepare-shutdown` は維持。終了予算: preStop 約 3 秒 + hook の agent 猶予 20 秒 + KILL → 60 秒内に収まる。
- README に終了シーケンス、予算、復旧手順、未確認事項を追記。

## 配備

- arch image が先に rollout されても旧 manifest でそのまま動く（tini は command の差のみ）。lolice manifest が先でも、image には tini が既にあるため問題ない。順序依存なし。
- Recreate rollout で Pod 全体が再作成されるため、active run がない時間帯に通常 GitOps で適用する。
