# etcd の書き込み churn 削減 (shanghai ノード突然死対策の D)

`boxp/arch` 側の PR と対になる変更。背景の完全版は
`boxp/arch` の `docs/project_docs/shanghai-node-resilience/plan.md` にある。

## 背景 (要約)

2026-08-01 20:50:45 UTC、control-plane ノード `shanghai-1`
(192.168.10.102 / Orange Pi Zero 3、rootfs = microSD) が突然ハングし、
**3日間** 無応答のままだった。電源は入っていたが同一 L2 から ARP INCOMPLETE。

Prometheus (31日保持) で追跡した結果、OOM・温度・ディスク残量・NIC エラーはすべて正常で、
停止直前まで全指標がフラットだった。真因は **microSD の I/O 破綻によるハードハング**:

| 日付 | shanghai-1 | shanghai-2 | shanghai-3 |
|---|---|---|---|
| 07/20 | 5.0 ms | 2.8 ms | 2.3 ms |
| **08/01 (死亡日)** | **9.5 ms** | 5.4 ms | 2.3 ms |

(write await = `node_disk_write_time_seconds_total / node_disk_writes_completed_total`, device=mmcblk0)

3ノードとも書き込み量は同一 (**~64 GiB/日**) なのに shanghai-1 の write await だけが劣化していた。
書き込みの主犯は **etcd** で、実測 13.8 MB/60s のユーザ空間書き込みが、
fsync と SD の消去ブロック粒度でブロック層 64 GiB/日 に増幅されている。

同じ死に方の証拠が shanghai-2 の 2026-08-03 00:13 の journal に残っている
(journald の書き込みが2分遅延 → `systemd-journald.service: Watchdog timeout (limit 3min)!`
→ CRI-O `DeadlineExceeded` → ログが途切れて凍結)。

**2026-08-19 追記 (INC-5)**: `shanghai-3` (192.168.10.104) が約 9 時間ハングし、物理再起動で復旧（SD カード交換不要）。根本原因は確定不能。微妙な点として、本計画の施策 A・B・D3 は boxp/arch PR #11944 (2026-08-05 マージ) で実装済みで、2026-08-17 CI apply でノードにも適用済みだった。ただし watchdog 設定にもかかわらず 9 時間の無応答が続いた点は未解明。推測: ①電源断（watchdogが効かない）または ② Ansible task ordering issue（journald.yml が node_resilience.yml より前に実行されるため、初回適用時に /var/log/journal が zram 上に作成→armbian-ramlog umount後に消失する可能性）。前回 boot の journal 消失については、journal persistent 設定が有効でも SD I/O 破綻でjournalバッファが flush できなかった可能性が高い。

## 本リポジトリでの対応 (D1 / D2)

### D1. etcd defrag の定期自動化 — `argoproj/etcd-defrag/`

2026-08-04 時点で etcd の DB は **626 MB のうち 94% が未使用**。
肥大した DB はページキャッシュを圧迫し、書き込み増幅を押し上げる。

毎週日曜 04:00 JST に `etcdctl defrag --cluster` を実行する CronJob を追加する。

実装上の制約と対処:

- **etcd イメージ (`registry.k8s.io/etcd:3.6.8-0`) は distroless でシェルを持たない。**
  実機で `sh` の不在を確認済み。よってシェルスクリプトによる逐次処理と健全性チェックは書けない。
  代わりに **initContainer を健全性ゲート**として使い、
  `etcdctl endpoint health --cluster` が失敗したら defrag を実行しない構成にした。
  既に 1 台落ちている状態で defrag を走らせると quorum を失うため、このゲートは必須。
- **`etcdctl defrag --cluster` はメンバーを 1 台ずつ順番に処理し、各メンバーの完了を待つ。**
  同時にブロックされるのは常に 1 台なので、3 メンバー構成では quorum が維持される。
- **既定の `--command-timeout=5s` では足りない。** Orange Pi Zero 3 上では
  MemberList だけで DeadlineExceeded になることを実機で確認したため、
  `--dial-timeout=15s` / health は 60s / defrag は 600s とした。
- 証明書は `healthcheck-client.crt` を使用 (実機で疎通確認済み)。
  秘密鍵が root:root 0600 のため `runAsUser: 0`。
  `/etc/kubernetes/pki/etcd` をディレクトリごと渡すと `server.key` / `peer.key` /
  (存在すれば) `ca.key` まで見えてしまうため、**必要な 3 ファイルだけ**を
  個別の read-only hostPath (`type: File`) で渡す。
- イメージは **manifest list digest で固定**
  (`registry.k8s.io/etcd:3.6.8-0@sha256:39718941...`)。
  etcd の秘密鍵を root で読むワークロードなので、キャッシュのないノードへ再スケジュールされた際に
  レビュー済みと異なるイメージを引かないようにする。
  `argoproj/argocd-image-updater/imageupdaters/README.md` の第三者イメージ方針に従う。
  multi-arch の manifest list なので control-plane の arm64 でも解決される。
- `hostNetwork: true` で `127.0.0.1:2379` を叩き、CNI に依存しない。
- `ttlSecondsAfterFinished: 86400` — 完了 Job を残し続けると、
  削減したいはずの etcd 書き込みを自分で増やしてしまう。

### D2. descheduler の実行間隔 — `argoproj/descheduler/helm/values.yaml`

`*/2 * * * *` (720回/日) → `*/30 * * * *` (48回/日)。

1回ごとに Job/Pod オブジェクトの生成・更新・削除が etcd に書かれる。
2分間隔で回し続ける価値は、microSD 上の control-plane では割に合わない。

## 対で入る変更 (boxp/arch 側)

- **A.** systemd hardware watchdog (`RuntimeWatchdogSec=30`) + `kernel.hung_task_panic=1`
  (`hung_task_timeout_secs=300`)。3日間の無応答を数分に縮める
- **B.** `armbian-ramlog` の無効化。`/var/log` が zram (RAM) 上にあったため、
  導入済みの `journald_persistent_storage` が実際には永続化されておらず、
  ハング時のログが毎回全消失していた
- **D3.** 稼働中の `/etc/kubernetes/manifests/etcd.yaml` へ `--listen-metrics-urls` を追加。
  PR #319 の `monitoring/etcd` ServiceMonitor は 3 ノードとも
  `2381: connection refused` で **scrape に失敗し続けていた**。
  実装時は `http://0.0.0.0:2381` にバインドすること。
  **`127.0.0.1:2381` (localhost) は不可** — Prometheus は別 Pod で動作するため
  ループバックインターフェースへは到達できず、localhost バインドでは scrape が引き続き失敗する。
  **アクセス制御の注意**: etcd は `hostNetwork: true` で動作するため、標準 Kubernetes NetworkPolicy は
  このポートに適用されない（NetworkPolicy は Pod ネットワーク上の通信にのみ適用）。
  2381/TCP を Prometheus のみに制限する手段として **ホスト側 iptables/nftables** がある。
  Calico 等の CNI では GlobalNetworkPolicy で host-network traffic を制御できる場合もあるが、
  適用可否は CNI 実装・バージョン・設定に依存するため、実環境で事前検証すること。
  なお etcd metrics は WAL fsync latency・backend commit latency 等を公開するが、
  `mmcblk0` の write await は `node_exporter` が担当する別レイヤーの指標である。

## 検証項目

- [ ] `kubectl -n kube-system get cronjob etcd-defrag` が作成される
- [ ] 手動実行 (`kubectl -n kube-system create job --from=cronjob/etcd-defrag etcd-defrag-manual`) が成功する
- [ ] 実行後 `etcdctl endpoint status --cluster` の DB SIZE が 626MB から大幅に縮む
- [ ] 実行中も `endpoint health --cluster` が 3/3 healthy を維持する
- [ ] descheduler の CronJob schedule が `*/30 * * * *` になる

## 残課題

- **C. etcd を microSD から外す (USB SSD)** — 本命の恒久対策。別途
- 2026-07-25 19:00 UTC に書き込みが 27 → 64 GiB/日 へ倍増した原因。
  3ノード同時・同幅だったため etcd 起因なのは確実だが、
  etcd メトリクスが未収集で犯人を特定できていない。D3 適用後に etcd WAL fsync /
  backend commit latency を確認して再調査する。
  なお `mmcblk0` の write await (block device 指標) は既存の `node_exporter` が収集済みで
  あり、D3 は etcd アプリケーション層の指標を追加するものである。
- **D3 のアクセス制御**: `--listen-metrics-urls=http://0.0.0.0:2381` は認証なしで
  全インターフェースに公開される。etcd が `hostNetwork: true` で動作するため、
  標準 Kubernetes NetworkPolicy は適用されない点に注意。
  ホスト側 iptables/nftables で 2381/TCP を制限する場合、**許可対象の送信元 IP は
  CNI/SNAT 設定に依存するため実装前に実測で確認すること**。Calico 環境では
  Pod → hostNetwork 宛の通信は通常 SNAT されず etcd ホストが観測する送信元は
  Prometheus の Pod IP (Pod CIDR 内) になる可能性が高いが、kube-proxy モードや
  CNI 設定によってはノード IP に SNAT される場合もある。
  実測手順: etcd ノード上で `tcpdump -i any -n port 2381` を実行した状態で
  Prometheus に scrape させ、実際の送信元 IP を確認してからルールを設定すること。
  （Calico GlobalNetworkPolicy を使う場合も host-network Pod への適用を事前に検証が必要）。
