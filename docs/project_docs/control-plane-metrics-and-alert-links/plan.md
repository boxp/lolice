# 通知の実用化: 誤検知の解消とメールリンクの修復

## 背景

`docs/project_docs/alertmanager-email-notifications/plan.md` で
critical アラートのメール通知が届くようになった。その最初の実運用で2つの問題が出た。

### 1. 最初に届いたのが1ヶ月前からの誤検知だった

```
KubeSchedulerDown          2026-07-04 00:18:02 UTC から firing
KubeControllerManagerDown  2026-07-04 00:18:07 UTC から firing
```

実機を確認したところ、コンポーネント自体は正常だった:

```
kube-scheduler / kube-controller-manager: --bind-address=127.0.0.1
listen: 127.0.0.1:10259 / 127.0.0.1:10257   ← ノード外から scrape できない
$ kubectl get endpoints -n kube-system kube-scheduler
Error from server (NotFound)
```

**kubeadm 構成の典型的な誤検知。** Prometheus から届かないだけ。
これを放置すると 6 時間ごとに誤検知メールが届き、
「Alertmanager からのメールは無視するもの」になる。せっかく直した通知経路が実質また死ぬ。

原因は2つあり、**両方揃わないと解消しない**:

- kubeadm 既定の `--bind-address=127.0.0.1` でノード外から届かない (本リポジトリ)
- kube-prometheus は ServiceMonitor しか持たず、対応する Service が無い。
  ServiceMonitor の selector は `app.kubernetes.io/name` だが、kubeadm の静的 Pod の
  ラベルは `component` (boxp/lolice 側)

### 2. メール本文のリンクが全て機能しない

Alertmanager と Prometheus の `externalUrl` がどちらも未設定だった。
未設定だと Pod のアドレス (`http://alertmanager-main-0:9093` 等) が使われるため、
メールクライアントからは開けない。

- 「View in AlertManager」/ silence リンク → Alertmanager の `externalUrl`
- 各アラートの Source リンク (generatorURL) → Prometheus の `externalUrl`

Cloudflare トンネルは既に `grafana.b0xp.io` と `prometheus-web.b0xp.io` を公開していたが、
**Alertmanager は公開されていなかった**。

## 対になる変更 (boxp/arch)

### terraform/cloudflare/b0xp.io/prometheus-operator

`alertmanager.b0xp.io` を既存トンネルに追加する。

| ファイル | 変更 |
|---|---|
| `dns.tf` | CNAME レコードを追加 |
| `tunnel.tf` | ingress に `http://alertmanager-main.monitoring.svc.cluster.local:9093` を追加 |
| `access.tf` | Access application + policy (GitHub ログイン) を追加 |

**Access による保護は必須。** Alertmanager はサイレンス操作ができ、
インフラ構成も見えるため、grafana / prometheus-web と同じ扱いにする。

### ansible

`control_plane_metrics.yml` を新設し、kube-scheduler / kube-controller-manager の
`--bind-address` を `0.0.0.0` にする。

10259/10257 は RBAC 認証付きなので、LAN へ bind しても未認証では読めない。

既定は `false`。control-plane playbook 側で明示的に有効化する
(`journald_persistent_storage` / `node_resilience_*` と同じ方式)。

静的 Pod ディレクトリ内にバックアップを作らない・変更前の原本を
`/var/backups/kubernetes-manifests/` へ退避する、という #11944 で確立した方式を踏襲する。
どちらのコンポーネントもリーダー選出で動くため、変更後は新しいアドレスで
listen するまで待ってから次のノードへ進む (playbook の `serial: 1` と組み合わせる)。

## 変更内容 (本リポジトリ)

- `control-plane-metrics-services.yaml` (新規) — ServiceMonitor が要求する
  `app.kubernetes.io/name` ラベルを持ち、静的 Pod の `component` ラベルを selector にする
  headless Service を kube-system に作る
- `overlays/alertmanager.yaml` — `externalUrl: https://alertmanager.b0xp.io`
- `overlays/prometheus.yaml` — `externalUrl: https://prometheus-web.b0xp.io`

## レビューで見つかった「これでは目的を達成できない」2件

### NetworkPolicy が cloudflared を弾く

`alertmanager-main` の NetworkPolicy は kube-prometheus 既定のままで、
Prometheus Pod (9093/8080) と Alertmanager 同士 (9094) しか許可していない。
トンネルを通しても cloudflared からの接続が拒否され、**メールのリンクは開けないまま**になる。
`overlays/network-policy-alertmanager.yaml` で cloudflared からの 9093 を許可する。
strategic merge では ingress リストが置き換わるため、既定の2エントリも明示的に書いている。

### kubeadm upgrade で bind-address が戻る (boxp/arch 側)

`kubeadm upgrade apply/node` は `--config` なしで実行され、ローカルファイルではなく
`kube-system/kubeadm-config` の ClusterConfiguration を読む。
稼働中マニフェストを直すだけでは次回アップグレードで誤検知が再発する。

## 検証項目

- [ ] `terraform validate` (実行済み: Success)
- [ ] `ansible-lint` (実行済み: pass)
- [ ] 適用後 `ss -lnt | grep 1025` がノード IP で listen していること
- [ ] `kubectl get endpoints -n kube-system kube-scheduler` に 3 エンドポイントが出ること
- [ ] Prometheus の kube-scheduler / kube-controller-manager target が up になること
- [ ] `KubeSchedulerDown` / `KubeControllerManagerDown` が resolved になること
- [ ] **メールのリンクを実際にクリックして開けること** (Access のログイン後)

最後の項目が本質。`externalUrl` を入れただけでは、トンネルが通っていなければ
リンクは開けない。
