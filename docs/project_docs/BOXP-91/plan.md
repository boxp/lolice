# BOXP-91: codex-workspace の計画 rollout 復旧

## 目的と完了条件

`codex-workspace` の image / Pod template 更新時も、RWO の `/home/boxp` PVC と singleton writer を増やさず、Task Board runner が中断 run を安全に閉じて 5 分以内に新規受付を再開できるようにする。SSH / Even Terminal、Obsidian 同期、draft watcher、Codex cron、Docker、dashboard と既存データは維持する。

## 現行構成の棚卸し

| container | 主な読み書き先 | 性質 | 採用構成 |
| --- | --- | --- | --- |
| `fetch-ssh-keys` init | `/home/boxp/.ssh`、`.pi/agent/models.json` | home 初期化 writer | Pod ごとに 1 回 |
| `install-docker-cli` init | `docker-cli` emptyDir | Pod 内 stateless | Pod ごとに 1 回 |
| `workspace` | home 全体、SSH / Even Terminal、認証状態 | 対話・永続 writer | singleton |
| `obsidian-sync` | Obsidian vault | continuous sync writer | singleton |
| `obsidian-task-board-draft` | vault の draft / ticket | watcher writer | singleton |
| `docker` | `docker-graph-storage` emptyDir | workspace / runner と localhost 結合 | Pod ごとに 1 個 |
| `codex-cron-scheduler` | vault の Cron 定義、home の agent 状態 | scheduler writer | singleton |
| `task-board-runner` | vault、`.codex-task-board`、run workspace | lane / lock / run writer | singleton |
| `task-board-dashboard` | `.codex-task-board` | read only | 理論上複製可能だが RWO と同居を維持 |

Service は `app=codex-workspace` の単一 Pod を選択し、image updater の Kustomize image 置換は同じ image を使う全 container を同一 Pod template rollout の対象にする。home PVC は `ReadWriteOnce` であり、Longhorn の volume replica 数は複数 process の同時書き込み安全性を提供しない。

## 比較と採用判断

1. runner の独立 Deployment 化は、同じ RWO PVC を複数 Pod が mount するため同一 node への暗黙依存が増える。runner は Docker daemon、home の agent 認証、vault sync とも結合しており、同じ image の自動更新対象でもあるため、今回の最小変更では採用しない。
2. dashboard 等の read-only 部分だけを複製しても Task Board 受付停止は改善せず、RWO volume の scheduling 制約だけが増えるため採用しない。
3. `replicas: 1` / `Recreate` を安全条件として維持し、Pod UID を runner lock owner に記録する。runner の SIGTERM shutdown hook と `preStop` が計画停止 marker を PVC へ書き、次 Pod が marker と owner の一致する lock だけを即時に `interrupted` 回収する方式を採用する。

`Recreate` により新 Pod は旧 Pod の終了後にだけ開始するため、計画停止 marker を持つ owner の process は回収時点ですでに存在せず、同一チケットを並行実行しない。marker がない fresh lock は回収しない。旧 image からの初回 rollout、SIGKILL、node 障害など shutdown hook と preStop の両方が動かない場合は heartbeat timeout を fallback とし、stale 判定を 180 秒、poll を 30 秒にして、最後の heartbeat から最大約 210 秒で再受付する。

## 実装

### boxp/arch

- lock に `CODEX_TASK_BOARD_OWNER_ID`（manifest 適用後は Pod UID、未設定時は Pod hostname）を記録する。
- `prepare-shutdown` command で owner-scoped marker を永続化する。
- loop process の SIGTERM shutdown hook からも同じ marker を永続化し、manifest hook がない場合も計画停止を通知する。
- startup / `recover` で marker と lock owner が一致する fresh lock を `:interrupted` にし、Notes に計画停止理由を残す。
- marker のない fresh active lock と、別 owner の lock は維持する。
- stale / corrupt lock の既存復旧と Task Board lane source-of-truth は変更しない。
- black-box test で即時回収、owner 不一致の非回収、owner metadata、lane/frontmatter/Notes の整合を検証する。

### boxp/lolice

- downward API で Pod UID を `CODEX_TASK_BOARD_OWNER_ID` に注入する。
- runner container の `preStop` から `prepare-shutdown` を実行する。
- `CODEX_TASK_BOARD_LOCK_STALE_SECONDS=180`、`CODEX_TASK_BOARD_POLL_SECONDS=30`、`terminationGracePeriodSeconds=60` を設定する。
- `replicas: 1` / `Recreate` を維持する。
- README に rollout / rollback、lock 診断、安全な手動復旧、残る単一障害点を記録する。

### cross-repository deployment contract

runner image の変更は [boxp/arch PR #11010](https://github.com/boxp/arch/pull/11010)、Deployment の変更は [boxp/lolice PR #723](https://github.com/boxp/lolice/pull/723) で同時にレビューする。lolice 側には `task-board-runner` の `preStop` から `prepare-shutdown` を呼ぶ設定、downward API の `metadata.uid` を `CODEX_TASK_BOARD_OWNER_ID` へ注入する設定、stale 180 秒 / poll 30 秒 / grace 60 秒が含まれる。PR #723 の ArgoCD diff と gitleaks は成功しており、render 上でもこれらの Deployment 差分を確認済みである。

適用順は arch → lolice とする。arch の image だけが先に rollout した場合、終了する旧 image は marker を作れないが、新 image の default stale 180 秒と既存 poll 60 秒により最大約 240 秒で復旧する。以後は runner 自身の SIGTERM hook が hostname owner marker を作り、lolice 適用後は Pod UID と preStop を加えた二重化された経路になる。

## 検証計画

- `boxp/arch/tests/codex-workspace/task-board-runner-test.sh` の全ケース。
- 一時 vault / state を使い、fresh heartbeat の旧 owner lock + shutdown marker を新 owner が即時回収し、同じ tick で再実行できることを時間計測する。
- loop process へ直接 SIGTERM を送り、preStop command なしでも owner / instance 一致の shutdown marker が作成されることを確認する。
- marker のない fresh lock、owner 不一致の marker、現在 heartbeat 中の lock が回収されないことを確認する。
- `kubectl kustomize argoproj/codex-workspace` と repository の Argo CD manifest validation を通す。
- render 後 manifest で replica / strategy、Pod UID env、preStop、timeout、既存 container / Service / PVC mount が維持されることを確認する。
- merge 後の実環境 rollout では旧 Pod UID、停止開始時刻、新 Pod Ready 時刻、planned recovery log、最初の新規 ticket 開始時刻を採取し、5 分以内と二重起動なしを確認する。

## rollout / rollback の安全条件

- rollout は通常の Argo CD sync / image updater に任せ、Pod の force delete は行わない。
- rollout 前に Deployment が `replicas: 1` / `Recreate` であることを確認する。
- rollback は両 repository の変更を revert する。runner image を先に戻す場合も lock の追加 key は EDN の未知 key として無害で、lolice manifest の preStop は対応 command を含む image と同時に戻す。
- lock の手動削除は、現 Pod UID と lock owner が不一致、該当 owner Pod が存在しない、heartbeat が stale であることを確認し、`task_board_runner.bb recover` が失敗した場合の最終手段に限定する。

## 残る単一障害点

- 単一 Pod、単一 RWO home PVC、配置 node、Kubernetes control plane / Longhorn、Obsidian remote、GitHub / agent API は HA 化しない。
- SSH / Even Terminal や個々の agent session の live migration は行わず、Pod 再作成中は短時間切断される。
- SIGKILL、node 障害など shutdown hook と preStop の両方が実行されない場合は owner marker を利用できないため、180 秒 heartbeat fallback で回収する。

## 検証記録（2026-07-10 UTC）

- 実環境 baseline: Deployment generation 89 / revision 83、`replicas=1`、`Recreate`、Pod `codex-workspace-858bcf8d45-zwswr`（UID `00aad169-dd98-4975-b339-a75c35efc2a5`）で 7 container が Ready。active lock は 1 秒間隔で heartbeat を更新していたが、現行 image の lock には owner metadata がなく、stale 設定は 1,800 秒だった。
- validation environment: 一時 vault / state、fake Codex、旧 owner の fresh lock と planned-shutdown marker を使う black-box rollout simulation は 1 秒で `interrupted` 記録、Notes 追記、新 owner lock 取得、同 tick 再実行、最終 lane/frontmatter 更新まで完了した。
- graceful signal: loop process に直接 SIGTERM を送り、preStop command を実行しない条件でも owner / instance 一致の shutdown marker が書かれる black-box test が通過した。
- active safety: 現 owner marker と owner-instance 不一致 marker の fresh lock は回収されず、新 run が開始されないテストが通った。replacement lock 取得後は旧 heartbeat / release が別 owner lock を更新・削除しない実装とした。
- `tests/codex-workspace/task-board-runner-test.sh`、runner 内蔵 test、`tests/codex-workspace/recurring-events-test.sh` が通過した。recurring-events の passwordless sudo を要する ownership test だけは環境条件により skip された。
- `kustomize build argoproj/codex-workspace` と、Calico NetworkPolicy 文書を除く client-side `kubectl apply --dry-run=client --validate=false` が通過した。Calico 文書は kubectl client の CRD patch 構造化変換制約のため render 成功で確認した。
- companion の lolice PR #723 で ArgoCD diff と gitleaks が成功し、rendered Deployment に `preStop prepare-shutdown`、Pod UID の `CODEX_TASK_BOARD_OWNER_ID`、stale 180 秒、poll 30 秒、grace 60 秒が現れることを確認した。
- 未 merge の manifest / image を実クラスタへ直接 apply すると実行中 Task Board run を中断するため、本 run では live rollout を行わず、上記 validation environment の再現テストを採用した。merge 後の初回 rollout は旧 lock に owner metadata がない可能性があるため 180 秒 fallback、次回以降は planned marker の即時回収を runbook に従って実測する。
