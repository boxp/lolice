# BOXP-270 Task Board → Hermes イベント駆動連携（lolice 側）

Obsidian 正本: `Projects/BOXP-270/README.md`。送信側実装は boxp/arch `docs/project_docs/BOXP-270/plan.md`。

## 目的

codex-workspace の Task Board runner が確定した遷移（Blocked / Review / Done、手動 Board 編集を含む）を、署名付き内部 Webhook で Hermes（コハコ）へ即時通知し、30 分 cron の全件 LLM 走査を縮小する。

## PR-A（本 PR）: 受信側と秘密共有、経路の開通

hermes-agent:
- `service-webhook.yaml`: ClusterIP `hermes-agent-webhook:8644`（公開入口は増やさない）。
- `networkpolicy.yaml`: Ingress policy を追加し、8644 は codex-workspace Pod からのみ許可、他の ingress は拒否。
- `external-secret-task-board-webhook.yaml`: ESO `Password` generator で HMAC secret をクラスタ内生成（`refreshInterval: "0"`、外部 store・手動値なし）。
- `rbac-task-board-webhook.yaml`: codex-workspace の `task-board-webhook-reader` SA にこの Secret 1 件の `get` のみ許可。
- `deployment.yaml`: `WEBHOOK_ENABLED=true`、`WEBHOOK_SECRET` を Secret から env 注入（配備版 v0.18.0 は env の global secret を全 route に適用）。init container が ConfigMap の route 定義を `/opt/data/webhook_subscriptions.json` に merge（route 自体に secret を持たせない、他 subscription は保持）。
- `configmap.yaml`: route `task-board`（events allowlist、名前付き payload 項目だけを使う prompt、skills obsidian-task-board、deliver telegram=home channel）。

codex-workspace:
- `service-account.yaml`: `task-board-webhook-reader`（Pod には mount しない）。
- `external-secret.yaml`: kubernetes provider の SecretStore（remoteNamespace hermes-agent）と ExternalSecret → Secret `codex-workspace-task-board-webhook`。
- `networkpolicy.yaml`: egress 8644 → hermes-agent Pod を許可。

## PR-B（後続）: codex-workspace Deployment に `task-board-event-dispatcher` sidecar

`/opt/codex-workspace/task-board/task_board_events.bb loop` を secret 付きの別 container で実行。runner container は secret を持たない。arch image の rollout 後に merge する。

## Hermes 側 config.yaml（PVC 上）の toolset 設定

`platform_toolsets.webhook: [terminal, file, session_search]` が必要（配備版の webhook 既定 toolset は web/vision/clarify のみで、ticket 読み取りと Notes 追記ができない。Telegram の既定 = core 全部より狭い）。init container が永続 config.yaml に冪等に追記する（未設定なら末尾へ block 追加、設定済みなら no-op、別値なら警告のみで書き換えない）。新規 bootstrap 用の ConfigMap 内 config.yaml にも同じ設定を含める。2026-10-22 に同内容を手動で先行適用済み（backup `/opt/data/config.yaml.bak-BOXP-270-20261022T104242Z`）。

## 配備版と公式仕様の差（v0.18.0 / ghcr.io/boxp/arch/hermes-agent:sha-b2bce92）

利用可: HMAC V2（timestamp ±300s）、rate limit、1 時間のメモリ内冪等、deliver telegram、[SILENT] 抑止、dynamic subscriptions hot reload。
未実装: `coalesce` / `filters` / `script` / `cron_job` / `mirror_to_session` / route `toolsets` / Standard Webhooks ヘッダ。したがって耐障害キュー・重複排除・静音延期は送信側（arch outbox）で持ち、返信文脈は Notes 記録 + Telegram session_search で継承する。

## 検証

- `kubectl kustomize argoproj/hermes-agent` / `argoproj/codex-workspace` が生成できる。
- merge 後: Secret 2 件が同値で生成される（値は表示しない、sha256 の一致のみ）、hermes gateway log に `routes: task-board`、codex-workspace から `hermes-agent-webhook.hermes-agent.svc:8644/health` が 200、未署名 POST が 401。

## Rollback

PR revert で Service / policy / Secret / env が消え、Hermes は route なし（従来どおり 0.0.0.0:8644 待受、route 0 件）に戻る。`webhook_subscriptions.json` の `task-board` エントリは `hermes webhook remove task-board` で削除。cron 410f33d091fc は本 PR で変更しない。
