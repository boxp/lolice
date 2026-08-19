# BOXP-176: shanghai-3 到達不能 — 原因調査と再発防止 (INC-5 post-incident)

## 概要

2026-08-19 02:10 UTC 頃、`shanghai-3` (192.168.10.104 / Orange Pi Zero 3 / control-plane) が
約 9 時間にわたって到達不能となった。物理再起動で復旧し、SD カード交換は不要だった。

本ドキュメントは BOXP-176 の調査結果と再発防止策のまとめ。
インシデントチケットは Obsidian vault の `Incidents/Tickets/INC-5.md` を参照。

## 調査結果

### 観測地点・時刻

| 時刻 (UTC) | 観測内容 | 観測地点 |
|---|---|---|
| 02:10:37 | shanghai-3 Node status 最終 heartbeat | Kubernetes API |
| 02:13:46 | Node Lease 最終 renewTime | Kubernetes API |
| 02:14:36 | Ready 条件が Unknown へ遷移 | Kubernetes API |
| 06:13 | SSH `192.168.10.104` → "No route to host" | codex-workspace Pod |
| 06:13 | kubectl get nodes → shanghai-3 NotReady | codex-workspace Pod |
| 06:14 | etcd endpoint health: `.102` / `.103` healthy, `.104` unhealthy (timeout) | shanghai-1 経由 SSH |
| 06:14 | readyz check passed (etcd ok) — VIP/API は正常 | in-cluster ServiceAccount |
| 11:30 | 物理再起動後 Calico / kubelet 起動 (dmesg / uptime より推定) | shanghai-3 SSH |
| 11:31 | Node Ready 条件が True に遷移 | Kubernetes API |
| 16:03 | 全 3 control-plane Ready, etcd 3/3 healthy 確認 | codex-workspace Pod |

### 現地調査結果 (再起動後)

- SD カード: SanDisk SA32G 29.1 GiB (manfid 0x000002)。現 boot で I/O エラーなし。
- `dmesg` および `journalctl -k` での I/O エラー確認: 現 boot では `mmcblk0` 関連の I/O エラーなし。
- `life_time` sysfs: SDHC カードは非対応のため取得不可。
- 前回 boot の journal: **消失**。`armbian-ramlog` が有効で `/var/log` が zram 上にあるため。
- Armbian には RTC バッテリーがないため起動時のクロックが不正確。journal の "first entry 02:00:04" は NTP 同期前の誤ったタイムスタンプ。実際の起動は `uptime` と dmesg から 11:30 UTC と推定。

### 根本原因

**確定不能**（前回 boot の journal 消失のため）。

最有力仮説: `docs/project_docs/shanghai-node-resilience/plan.md` に記録された **microSD I/O 破綻によるカーネル・ハードハング** と同一パターン。

- etcd WAL の書き込みが fsync + SD 消去ブロック粒度で ~64 GiB/日 に増幅。
- SD カードの write await が累積的に悪化。
- journald の書き込み遅延が 3 分を超えて `systemd-journald: Watchdog timeout!`。
- CRI-O が `DeadlineExceeded`、OS が完全フリーズ（ARP INCOMPLETE 状態）。
- 物理再起動で復旧（SD カード完全損傷ではなく一時的なフリーズ）。

### VIP / Kubernetes API との分離確認

- VIP (`192.168.10.99:6443`) は kube-vip が `shanghai-1` で保持しており、API は正常応答。
- `shanghai-3` の IP/ポートへの直接接続のみ不通。
- 外部観測点からの TCP timeout は API 障害ではなく経路/FW/ノード固有の問題と判断済み。

### etcd quorum 確認

- 障害中: `.102` `.103` の 2/3 member healthy。quorum 維持。
- 復旧後: 3/3 member healthy 確認済み。

## 再発防止策

`docs/project_docs/shanghai-node-resilience/plan.md` に定義済みの施策のうち、**A / B / D3 が未適用のまま再発した**。
D1（etcd defrag CronJob）と D2（descheduler 間隔削減）は commit #115db69 (#761) で実装済みだった。
残存する未適用施策 A / B / D3 を優先的に実施する。

### 緊急 (arch リポジトリ側)

- **[A] systemd hardware watchdog**  
  `RuntimeWatchdogSec=30` + `kernel.hung_task_panic=1` (`hung_task_timeout_secs=300`)  
  効果: フリーズを数分以内に自動再起動に変える。3日間の無応答 → 5分以内の自動復旧。

- **[B] armbian-ramlog の無効化 + journal 永続化**  
  `/etc/systemd/journald.conf` に `Storage=persistent` を設定。  
  効果: クラッシュ直前のログが次回起動後に参照可能になり、根本原因特定が可能になる。

- **[D3] etcd `--listen-metrics-urls` 追加**  
  `/etc/kubernetes/manifests/etcd.yaml` に `--listen-metrics-urls=http://0.0.0.0:2381` を追加する。  
  **注意: `127.0.0.1:2381` (localhost バインド) は不可**。Prometheus は別 Pod で動作するため
  ループバックインターフェースには到達できず、localhost バインドでは scrape が引き続き失敗する。
  そのためノード IP へのバインド (`0.0.0.0:2381` または各ノード固有 IP) が必須。  
  **アクセス制御**: etcd は `hostNetwork: true` で動作するため、標準 Kubernetes NetworkPolicy は
  このポートには適用されない。ホスト側 iptables/nftables で 2381/TCP を制限する場合、
  **許可対象の送信元 IP は CNI/SNAT 設定に依存するため、実装前に実測で確認すること**。
  Calico を使用する環境では Pod → hostNetwork 宛の通信は通常 SNAT されず、
  iptables が観測する送信元は Prometheus Pod IP (Pod CIDR 内) となる可能性が高い。
  ただし kube-proxy モードや CNI 設定によってはノード IP に SNAT される場合もある。
  実測手順: etcd ノード上で `tcpdump -i any -n port 2381` を実行した状態で
  Prometheus に scrape させ、実際の送信元 IP を確認してからルールを設定すること。
  （Calico GlobalNetworkPolicy でも代替可能だが host-network Pod への適用を事前に検証すること）。  
  効果: Prometheus が etcd の WAL fsync latency・backend commit latency を収集し、etcd 書き込み性能の悪化を早期検知できる。  
  注: `mmcblk0` の write await (block device 指標) は `node_exporter` の block-device メトリクス
  (`node_disk_write_time_seconds_total` 等) で別途収集する。D3 は etcd アプリケーション層の
  指標を補完するものであり、block device 層は node_exporter が担当する。

### 高 (本リポジトリ = lolice)

- **[D1] etcd defrag CronJob** — `argoproj/etcd-defrag/`  
  毎週日曜 04:00 JST に `etcdctl defrag --cluster` を実行。  
  etcd DB の肥大化（94% が未使用）を解消し、書き込み増幅を削減。

- **[D2] descheduler 実行間隔削減** — `argoproj/descheduler/helm/values.yaml`  
  `*/2 * * * *` → `*/30 * * * *`。etcd への不要書き込みを 1/15 に削減。

### 長期

- **[C] etcd データディレクトリを USB SSD へ移行**  
  `/var/lib/etcd` を microSD から外す。本命の恒久対策（別途チケット化）。

## 現在のアクション状況

- [x] INC-5 インシデントチケット作成 (`Incidents/Tickets/INC-5.md`)
- [x] Incident Board 更新 (Monitoring レーンに INC-5 追加)
- [x] Runbook 更新 (`Incidents/Runbooks/shanghai-control-plane-sdcard-failure.md`): transient hang vs SD 完全損傷の判定フロー、インシデント履歴表、prevention tasks を追記
- [x] `docs/project_docs/shanghai-node-resilience/plan.md` に INC-5 の再発を記録
- [ ] arch リポジトリ: A / B / D3 の実装 PR (boxp への作業依頼)
- [x] lolice リポジトリ: D1 / D2 の実装 — commit #115db69 (#761) で実装済み
