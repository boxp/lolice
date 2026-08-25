# BOXP-181: Alertmanager 通知設計の本番調整

## 分類結果（2026-08-25 04:57Z の通知群）

| 対象 | GitOps desired state | live 状態・観測 | 判断と対処 |
| --- | --- | --- | --- |
| grafana-alloy | `default` の ServiceMonitor は lolice 管理外 | DaemonSet は 7/7 Ready。ServiceAccount 不在の FailedCreate event あり | Prometheus を `release=prometheus` opt-in に変更し、未所有 Monitor を収集対象外にする。 |
| stage-hitohub TiDB | PD/TiDB/TiKV は各 1 replica | PD と TiDB が CrashLoopBackOff | 必要サービスの障害。通知を抑制せず、別の原因調査チケットで追跡する。 |
| stable-diffusion | WebUI と cloudflared は `replicas: 0` | 0/0 | 意図的停止。専用 PrometheusRule を desired state から除去する。 |
| prod-hitohub cloudflared | Deployment は `replicas: 0` | 0/0 | 意図的停止。静的 scrape target を desired state から除去する。 |
| Longhorn Job | Longhorn が生成する recurring job | 2025-08-10 に失敗した Job が残存、現行 jobs は完了 | 廃止済み失敗 Job を削除し、Longhorn の GitOps ServiceMonitor には opt-in を明示する。将来の job failure は抑制しない。 |
| local-llm | llama-server は 1 replica、ServiceMonitor は GitOps 管理 | llama-server が CrashLoopBackOff、scheduler/resource と readiness failure events あり | 必要サービスの障害。ServiceMonitor を opt-in し、通知は維持して別チケットで原因調査する。 |

## 通知設計

- ServiceMonitor は `release=prometheus` を明示したものだけを収集する。local-llm と Longhorn は GitOps desired state で label を付与する。
- warning は `alertname, namespace` 単位で集約し、初回 15 分待機、group interval 6 時間、repeat 7 日にする。既存 warning を設定反映直後に一斉送信しない。
- critical は 30 秒待機・5 分 group interval・6 時間 repeat を維持する。ControlPlaneNodeNotReady、etcd quorum/healthy endpoint と snapshot 系の critical を warning の抑制対象にしない。

## 検証

1. `scripts/verify-alertmanager-template.py` で ESO template とレンダリング YAML を検証する。
2. `kubectl kustomize argoproj/prometheus-operator` と各変更アプリの Kustomize render を確認する。
3. merge 後に Argo CD sync revision、Prometheus の ServiceMonitor discovery、Alertmanager config/reload と alert evaluation を確認する。
