# BOXP-40 Grafana Alert Notifications

## 方針

SMTP は現環境では使わないため、email 通知は復旧対象外にする。Grafana Alerting の Discord 通知だけを GitOps 管理に移し、既存 UI 設定に依存して pause されたままにならない状態を作る。

## 実態確認

- 2026-07-04 01:31 UTC: Grafana API は到達可能。version 13.0.2 / database ok。
- 2026-07-04 01:31 UTC: Grafana-managed alert rule 4 件はすべて `isPaused: true`。Prometheus-compatible rules API でも `lastEvaluation: 0001-01-01T00:00:00Z` で、通知以前に評価が止まっていた。
- 2026-07-04 01:31 UTC: Discord contact point は `boxlab alert` と `onitiku server` の 2 件。email contact point も存在するが、SMTP は使わないため今回の GitOps 対象外。
- 2026-07-04 01:31 UTC: default notification policy は `boxlab alert`。rule-level receiver は hitohub 2 件が `boxlab alert`、ark/palserver が `onitiku server`。
- 2026-07-04 01:32 UTC: active silence と mute timing は 0 件。
- 2026-07-04 01:32 UTC: `monitoring/grafana` NetworkPolicy は egress 全許可。Discord webhook への外向き通信を repo 上で明示的に止める設定は見つからなかった。
- 2026-07-04 01:32 UTC: 現在の Grafana service account token では contact point test API と alert rule write API が 403。Secret 参照と Pod log 参照も Kubernetes RBAC で不可。

## 変更内容

- `argoproj/prometheus-operator/grafana-alerting.yaml`
  - Discord contact point 2 件を provisioning 化する。
  - default notification policy を `boxlab alert` として provisioning 化する。
  - 既存 alert rule 4 件を provisioning 化し、`isPaused: false` にする。
  - alert rule ごとの `notification_settings.receiver` は現行実態に合わせる。
- `argoproj/prometheus-operator/grafana-discord-webhooks.yaml`
  - Discord webhook URL を ExternalSecret で `Secret/grafana-discord-webhooks` に同期する。
  - SSM Parameter Store に次の SecureString が必要:
    - `/lolice/grafana/discord-webhook-boxlab-alert`
    - `/lolice/grafana/discord-webhook-onitiku-server`
- `argoproj/prometheus-operator/overlays/grafana.yaml`
  - Grafana に Discord webhook URL を環境変数として注入する。
  - `/etc/grafana/provisioning/alerting` に provisioning ConfigMap を mount する。
  - Reloader annotation を付与し、ConfigMap / Secret 更新時に Grafana Pod を再起動させる。
- Grafana 公式 docs では既存の alerting resource を file provisioning に import すると競合する場合があるため、初回適用時に Grafana Pod log で provisioning conflict が出た場合は、同 UID の UI 管理 rule/contact point を削除してから再 sync する。

## 適用後の確認手順

1. SSM Parameter Store に Discord webhook URL 2 件を登録する。値はチケット、PR、ログに出さない。
2. Argo CD で `prometheus-operator` Application を sync する。
3. `ExternalSecret/grafana-discord-webhooks` が Ready になり、`Secret/grafana-discord-webhooks` が作成されたことを確認する。
4. Grafana Pod が再起動し、provisioning エラーなく起動したことを確認する。
5. Grafana API または UI で alert rule 4 件が `isPaused: false` になり、`lastEvaluation` が現在時刻に更新されることを確認する。
6. Discord contact point test または安全な一時テスト alert で、`boxlab alert` と `onitiku server` の両方の Discord 受信時刻を記録する。

## 残課題

- この Codex workspace の権限では Discord test notification と Grafana rule write が 403 のため、実送信確認は未完了。
- SSM Parameter Store への Discord webhook URL 登録が必要。未登録のまま Argo CD sync すると Grafana Pod は Secret 参照待ちになる。
- 初回適用時に既存 UI 管理リソースとの provisioning conflict が出る可能性がある。
