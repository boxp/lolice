# BOXP-40: Grafana Alert 通知復旧

## 調査結果

- 2026-07-03 19:20 UTC 頃、Codex workspace から Grafana API (`http://grafana.monitoring.svc.cluster.local:3000`) に到達できることを確認した。
- Grafana v13.0.2 の Grafana-managed alert rule は 4 件存在するが、全件 `isPaused: true` だった。Prometheus-compatible rules API でも `lastEvaluation: 0001-01-01T00:00:00Z` のため、発火以前に評価が止まっている。
- active alert は 0 件、silence は 0 件、mute timing は 0 件だった。
- contact point は Discord 2 件、email 1 件が存在する。default notification policy の receiver は `boxlab alert` のみで、email contact point は policy tree に route されていない。
- Grafana runtime settings の `smtp` は未設定だった。email contact point が存在しても、SMTP 未設定のままではメール送信できない。
- Kubernetes API 権限上、Codex workspace service account から `monitoring` namespace の Secret 一覧と Grafana Pod log 取得は Forbidden だった。
- Grafana contact point test API は現在の service account token では `alert.notifications.receivers:test` / secrets read 系 permission が不足しており、Discord と email の実送信確認は実行できなかった。

## 実装方針

- `boxp/lolice` の宣言値に Grafana SMTP Secret 参照を追加し、SMTP 設定をクラスタ Secret から Grafana env override (`GF_SMTP_*`) として注入する。
- Secret 値はリポジトリに置かず、External Secrets 経由で SSM Parameter Store から同期する。
- SMTP Secret は必須参照にし、未設定のまま Grafana が SMTP なしで起動しないようにする。
- `secret.reloader.stakater.com/reload: "grafana-smtp"` により、SMTP Secret 更新時に Grafana Pod を再起動させる。

## 追加する SSM Parameter

以下を SecureString または運用上適切な型で登録する。

- `/lolice/grafana/smtp-enabled`: `true`
- `/lolice/grafana/smtp-host`: SMTP host と port。例: `smtp.example.com:587`
- `/lolice/grafana/smtp-user`: SMTP user
- `/lolice/grafana/smtp-password`: SMTP password
- `/lolice/grafana/smtp-from-address`: Grafana alert の送信元アドレス

これらの parameter を登録しないまま Argo CD sync すると Grafana Pod が Secret 参照待ちになるため、sync 前に登録する。

## 残作業

1. Grafana の alert rule 4 件が意図通りであれば pause を解除する。
2. email contact point を default policy の route に追加するか、Discord と email を同じ contact point グループにまとめる。
3. test notification を Discord と email の両方に送信し、受信時刻と宛先種別をチケット Notes に記録する。
4. 必要なら、既存 UI/DB 管理の contact point / notification policy / alert rule を file provisioning または Terraform 管理へ移行する。移行時は既存 resource との import conflict に注意する。

## 今回の検証

- `kubectl config current-context`: `in-cluster`
- `curl $GRAFANA_URL/api/health`: database ok, version 13.0.2
- Grafana API:
  - `/api/v1/provisioning/alert-rules`
  - `/api/prometheus/grafana/api/v1/rules`
  - `/api/v1/provisioning/contact-points`
  - `/api/v1/provisioning/policies`
  - `/api/v1/provisioning/mute-timings`
  - `/api/alertmanager/grafana/api/v2/alerts`
  - `/api/alertmanager/grafana/api/v2/silences`
- `kustomize build argoproj/prometheus-operator`
