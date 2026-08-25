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

**2026-08-19 追記 (INC-5)**: `shanghai-3` (192.168.10.104) が約 9 時間ハングし、物理再起動で復旧（SD カード交換不要）。根本原因は確定不能。施策 A・B・D3（メトリクスURL設定部分）は boxp/arch PR #11944 (2026-08-05 マージ) で設定済みで、2026-08-17 CI apply でノードにも適用済みとされていた。ただし watchdog 設定にもかかわらず 9 時間の無応答が続いた点は未解明だった。

**2026-08-25 追記 (実効性確認)**:
全 3 control-plane で設定を実測確認した結果、**A・B は実際には未動作であることが判明**した。

- **A (watchdog)**: `/etc/systemd/system.conf` の `RuntimeWatchdogSec` はコメントアウト状態（`#RuntimeWatchdogSec=off`）、kubelet `WatchdogUSec=0` より、全 3 ノードで watchdog は設定未適用と推定される。なお `systemctl show --property=RuntimeWatchdogUSec` および `/dev/watchdog` デバイスの直接確認は実施できなかったため断定は避ける。INC-5 での 9 時間無応答への寄与は考えられるが、単一主因と断定するには証拠が不十分。
- **B (journal 永続化)**: `journald.conf` の `Storage=volatile` が全ノードで有効なまま。`/var/log/journal/` ディレクトリは存在するが、`Storage=volatile` 優先で RAM にしか書かれない。ハードリブート時のログ全消失は防げていない。
- **D3 (etcd metrics)**: ポート 2381 は実際に応答確認済み。アクセス制御は BOXP-177 で追跡中。

2026-08-25 時点の etcd WAL fsync p99: shanghai-1 **84.5 ms**（⚠️）、shanghai-2 10.8 ms、shanghai-3 13.0 ms。
mmcblk0 平均書き込みレイテンシ: shanghai-1 **8.42 ms**（他の約 3 倍）、shanghai-2/3 は ~2.7 ms。
shanghai-1 の I/O 劣化が継続しており、INC-2 (2026-08-01) 時と同様のパターン。

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

- **A.** systemd hardware watchdog (`RuntimeWatchdogSec=15`) + `kernel.hung_task_panic=1`
  (`hung_task_timeout_secs=300`)。3日間の無応答を数分に縮める。
  **⚠️ 2026-08-25 実測確認: 全ノードで設定未適用（推定）。`system.conf` の `RuntimeWatchdogSec` がコメントアウト状態のまま、`systemctl show --property=RuntimeWatchdogUSec` による直接確認は未実施。要修正。**
- **B.** `armbian-ramlog` の無効化 + `Storage=persistent` 設定でジャーナル永続化。
  **⚠️ 2026-08-25 実測確認: 全ノードで `Storage=volatile` のまま未修正。ハングログが消失し続けている。要修正。**
- **D3.** etcd `--listen-metrics-urls=http://0.0.0.0:2381` 追加 (メトリクスURL: 実装済み / **アクセス制御: 未実施**)  
  boxp/arch PR #11944 でメトリクス URL を追加し、2026-08-17 全ノードに適用済み。  
  **⚠️ 残作業 (BOXP-177)**: 現在 2381/TCP は認証なしで全クライアントからアクセス可能。
  etcd が `hostNetwork: true` のため標準 NetworkPolicy 非適用。
  Prometheus からの scrape を実測確認後、ホスト側 iptables/nftables または Calico GlobalNetworkPolicy で
  Prometheus のみに制限する必要がある。
  実測手順: etcd ノード上で `tcpdump -i any -n port 2381` を実行して Prometheus の実際の送信元 IP を確認してからルールを設定すること。
  なお etcd metrics は WAL fsync latency・backend commit latency を公開し、
  `mmcblk0` の write await は `node_exporter` が担当する別レイヤーの指標である。

## 検証項目

- [ ] `kubectl -n kube-system get cronjob etcd-defrag` が作成される
- [ ] 手動実行 (`kubectl -n kube-system create job --from=cronjob/etcd-defrag etcd-defrag-manual`) が成功する
- [ ] 実行後 `etcdctl endpoint status --cluster` の DB SIZE が 626MB から大幅に縮む
- [ ] 実行中も `endpoint health --cluster` が 3/3 healthy を維持する
- [ ] descheduler の CronJob schedule が `*/30 * * * *` になる

## 残課題

- **⚠️ A・B の実効性修正（最優先）**: boxp/arch 側の Ansible playbook で `RuntimeWatchdogSec=15` と `Storage=persistent` が実際のノード設定に反映されるよう調査・修正が必要。CI apply は成功しているが実ノードに設定が適用されていない。
- **C. etcd を microSD から外す (USB SSD)** — 本命の恒久対策。別途チケット化。
- **D3 アクセス制御 (BOXP-177)**: `--listen-metrics-urls=http://0.0.0.0:2381` は現在認証なし・アクセス制御なしで公開中。
  etcd が `hostNetwork: true` のため標準 NetworkPolicy は適用不可。
  ホスト側iptables/nftablesまたはCalico GlobalNetworkPolicyで制御する必要がある（BOXP-177で追跡中）。
- **etcd/mmcblk0 アラートルール**: 2026-08-25 に WAL fsync p99 や mmcblk0 write latency の実測値を取得。これらを閾値とした PrometheusRule の実装が必要（未実装）。
- **2026-07-25 書き込み倍増の原因**: 27 → 64 GiB/日 へ倍増した原因。D3 で etcd WAL fsync latency が収集できるようになったため、再調査可能。
