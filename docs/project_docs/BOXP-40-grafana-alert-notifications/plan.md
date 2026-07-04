# BOXP-40 Grafana alert notifications

## Scope

Grafana Alerting の通知不達を調査し、Discord 通知経路を復旧する。

2026-07-04 の方針変更により、SMTP / email は対象外とする。Grafana に email contact point は残っているが、クラスタ側に SMTP 設定がないため、本タスクでは Discord のみを復旧対象にする。

## Cluster findings

- 2026-07-04 01:31 UTC: Grafana API は `http://grafana.monitoring.svc.cluster.local:3000` で到達可能。`/api/health` は `database: ok`、Grafana `13.0.2`。
- 2026-07-04 01:31 UTC: Grafana-managed alert rule は 4 件。すべて `isPaused: true` で、Prometheus-compatible rules API の `lastEvaluation` は `0001-01-01T00:00:00Z`。通知経路以前に alert evaluation が止まっている。
- 2026-07-04 01:31 UTC: active silence は 0 件。mute timing も 0 件。
- 2026-07-04 01:31 UTC: notification policy の default receiver は `boxlab alert`。`group_by` は `grafana_folder`, `alertname`。default policy 自体は Discord contact point へ向いている。
- 2026-07-04 01:31 UTC: contact point は Discord 2 件、email 1 件。
  - `boxlab alert`: Discord
  - `onitiku server`: Discord
  - `email receiver`: email
- 2026-07-04 01:32 UTC: rule-level notification settings は以下。
  - `ark asa server restarts count`: `onitiku server`
  - `onitiku palworldが稼働していません`: `onitiku server`
  - `hitohub-frontend state running count`: `boxlab alert`
  - `hitohub-backend state running count`: `boxlab alert`
- 2026-07-04 01:32 UTC: Grafana Pod は `monitoring/grafana-6f888dc7f6-hh8bj`、`1/1 Running`。Deployment image は `grafana/grafana:13.0.2`。
- 2026-07-04 01:32 UTC: `monitoring/grafana` NetworkPolicy は egress 全許可。Discord webhook への外向き通信を明示的に止める NetworkPolicy は確認されなかった。
- 2026-07-04 01:32 UTC: Grafana Deployment の env は空で、SMTP 設定は注入されていない。email は本タスク対象外。

## Blockers

- 2026-07-04 01:31 UTC: Grafana contact point test API は現在の service account token では 403。必要権限として `alert.notifications.receivers:test` / `alert.notifications.receivers.secrets:read` などが不足している。
- 2026-07-04 01:32 UTC: alert rule の pause 解除を provisioning API で試したが 403。必要権限として `alert.rules:write` / `alert.rules.provisioning:write` / `alert.provisioning.provenance:write` などが不足している。
- 2026-07-04 01:32 UTC: Kubernetes service account は `monitoring` namespace の Pod 参照は可能だが、Secret 参照と Pod log 参照はできない。そのため Discord webhook URL の有効性確認と Grafana notification delivery log の確認は未実施。

## Required recovery steps

1. Grafana 管理者、または上記 Grafana Alerting 権限を持つ service account で、4 件の alert rule の pause を解除する。
2. 解除後、各 rule の `lastEvaluation` が現在時刻へ更新されることを確認する。
3. `boxlab alert` と `onitiku server` の Discord contact point で test notification を実行し、Discord 側で受信を確認する。
4. test notification が失敗する場合は、Grafana Pod log と Discord webhook URL の有効性を確認する。Secret 値や webhook URL はチケット、PR、ログへ残さない。
5. Discord で実受信できた UTC 時刻と receiver 名を作業ログへ追記する。

## Repository changes

- 今回は SMTP / email の宣言値変更は行わない。
- 現時点で Discord contact point、notification policy、Grafana-managed alert rule は Grafana DB 管理に見える。`boxp/lolice` の Kubernetes / Argo CD / Helm 宣言値として修正できる対象は確認できていない。
- 前回 run の PR `#682` は SMTP wiring を追加しているが、2026-07-04 の方針変更後は不要。
