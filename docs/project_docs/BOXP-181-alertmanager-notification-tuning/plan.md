# BOXP-181: Alertmanager 通知設計の本番調整

## 分類結果（2026-08-25 04:57Z の通知群）

| 対象 | GitOps desired state | live 状態・観測 | 判断と対処 |
| --- | --- | --- | --- |
| grafana-alloy | GitOps 管理の ServiceMonitor を使用 | DaemonSet は 7/7 Ready。ServiceAccount 不在の FailedCreate event あり | ServiceMonitor に `app.kubernetes.io/part-of=kube-prometheus` を明示し、収集を継続する。未所有 `default/grafana-alloy` は opt-in label がないため収集しない。 |
| stage-hitohub TiDB | PD/TiDB/TiKV は各 1 replica | PD と TiDB が CrashLoopBackOff | 2026-08-25のサービスクローズ判断により監視廃止対象。workload/PVC/データは保持し、Prometheus収集のみ停止する。 |
| stable-diffusion | WebUI と cloudflared は `replicas: 0` | 0/0 | 意図的停止。専用 PrometheusRule を desired state から除去する。 |
| prod-hitohub cloudflared | Deployment は `replicas: 0` | static scrape target が TargetDown | サービスクローズ済み。stage/prod の static scrape target を含む ScrapeConfig 全体を desired state から除去する。 |
| Longhorn Job | Longhorn が生成する recurring job | 2025-08-10 に失敗した Job が残存、現行 jobs は完了 | 廃止済み失敗 Job を削除し、Longhorn の GitOps ServiceMonitor には opt-in を明示する。将来の job failure は抑制しない。 |
| local-llm | llama-server は 1 replica、ServiceMonitor は GitOps 管理 | llama-server が CrashLoopBackOff、scheduler/resource と readiness failure events あり | 必要サービスの障害。ServiceMonitor を opt-in し、通知は維持して別チケットで原因調査する。 |

## 通知設計

- ServiceMonitor は `app.kubernetes.io/part-of=kube-prometheus` を opt-in ラベルとする。kube-prometheus v0.18.0 同梱の基盤 Monitor と、GitOps 管理の local-llm、Longhorn、Intel GPU exporter、Grafana Alloy、Loki、etcd に同じラベルを付与する。これ以外の未所有 Monitor は収集しない。Kubernetes の `matchExpressions` は複数条件を AND 結合するため、OR を期待した selector は使用しない。
- warning は `alertname, namespace` 単位で集約し、初回 15 分待機、group interval 6 時間、repeat 7 日にする。親 route は `Default` のままとし、`severity=warning` のみを Warning receiver に送るため、info を設定反映直後に一斉送信しない。
- critical は 30 秒待機・5 分 group interval・6 時間 repeat を維持する。ControlPlaneNodeNotReady、etcd quorum/healthy endpoint と snapshot 系の critical を warning の抑制対象にしない。

## hitohub 監視廃止（2026-08-25 06:06Z の明示判断）

- GitOps と live state を再棚卸しした結果、hitohub 専用の Prometheus 定義は `monitoring/static-config` の stage/prod cloudflared static target だけだった。`ServiceMonitor`、`PodMonitor`、`PrometheusRule`、`Probe`、Grafana dashboard、CronJob/外形監視には hitohub 専用定義がない。
- `scrape-config.yaml` と Kustomize の参照を削除し、Argo CD sync で live `ScrapeConfig/static-config` を prune する。これにより stage/prod の cloudflared `TargetDown` と TiDB を含む hitohub 系メトリクス・アラートの評価対象を除外する。
- `stage-hitohub` / `prod-hitohub` Application と各ワークロード、PVC、TiDBデータ、ImageUpdater は監視定義ではないため変更しない。Application を削除すると prune によりワークロードを消すおそれがあり、本チケットの制約に反する。

## 検証

1. `scripts/verify-alertmanager-template.py` で ESO template とレンダリング YAML を検証する。
2. `kubectl kustomize argoproj/prometheus-operator` と各変更アプリの Kustomize render を確認し、render結果に `ScrapeConfig/static-config` と hitohub target が含まれないことを確認する。
3. merge 後に Argo CD sync revision、live `ScrapeConfig/static-config` の削除、Prometheus target/alert evaluation、Alertmanager config/reload を確認する。
