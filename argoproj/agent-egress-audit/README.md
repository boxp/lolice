# agent-egress-audit（BOXP-721）

Hermes（`hermes-agent`）と taskrunner（`codex-workspace`）の通信先を **記録だけする** 最小構成。遮断はしない。
現行仕様は Obsidian `Projects/agent-egress-audit/spec.md`。

## 構成

| 部品 | 置き場所 | 記録するもの |
|---|---|---|
| agent 専用 DNS resolver（CoreDNS 2 台、`10.96.0.53`） | この app | cluster.local・逆引き以外の問合せ: 問合せ元 IP・種別・名前・応答コード・応答サイズ・所要時間 |
| Calico NetworkPolicy の `action: Log` | `hermes-agent/networkpolicy.yaml`、`codex-workspace/networkpolicy.yaml` | Pod CIDR 外宛ての新しい接続の最初のパケットのヘッダ（送信元/宛先 IP・port・protocol） |
| Alloy | `grafana-alloy/configmap.yaml` | DNS 名の 24 文字以上のラベルを `<redacted-label-len=N>` に置換して Loki へ。kernel journal から `calico-packet:` 行だけを Loki へ |

## 見方（Grafana Explore → Loki）

```logql
{namespace="agent-egress-audit", container="coredns"}          # DNS
{job="agent-egress-audit/conn"}                                 # 接続
```

停止・欠測の確認（Prometheus）:

```promql
sum(increase(loki_process_custom_agent_egress_dns_log_lines_total[1h]))
sum by (instance) (increase(loki_process_custom_agent_egress_conn_log_lines_total[1h]))
kube_deployment_status_replicas_available{namespace="agent-egress-audit"}
```

## 戻し方（どれも単独で戻せる）

1. agent Pod の DNS を元に戻す: `hermes-agent/deployment.yaml`（と `codex-workspace/deployment.yaml`）の `dnsPolicy: None` と `dnsConfig` を削除する。Pod が再作成され kube-dns 直結に戻る。
2. 接続記録を止める: 2 つの networkpolicy.yaml から `action: Log` のルールを削除する（Pod 再起動なし）。
3. resolver を撤去する: 1 の後に `argoproj/kustomization.yaml` から `agent-egress-audit/application.yaml` を外し、Application を削除する。
4. Alloy: `grafana-alloy/configmap.yaml` の `BOXP-721` の節を削除する。

resolver が止まっても agent の名前解決は kube-dns へ退避して続く（1 問合せあたり最大数秒遅くなる）。
