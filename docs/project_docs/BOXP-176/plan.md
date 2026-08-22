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
- 前回 boot の journal: **消失**。`armbian-ramlog` は boxp/arch PR #11944 (2026-08-05) で無効化済みのはずだが、再起動後の実測で `/etc/default/armbian-ramlog` の `ENABLED=false` と `/var/log/journal` の存在を確認できておらず、SDカードI/Oエラーによりjournaldバッファがdisk flushされる前にクラッシュした可能性が高い。
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

`docs/project_docs/shanghai-node-resilience/plan.md` に定義済みの施策の適用状況。
**A / B / D1 / D2 は実装済み。D3 はメトリクス URL 設定のみ実装済みで、アクセス制御は未実施（BOXP-177 で追跡中）。**

| 施策 | 実装場所 | 実装日 | ノード適用日 | 備考 |
|---|---|---|---|---|
| A (watchdog) | boxp/arch PR #11944 | 2026-08-05 | 2026-08-17 CI apply ✓ | 完了 |
| B (armbian-ramlog無効化 + journal永続化) | boxp/arch PR #11944 | 2026-08-05 | 2026-08-17 CI apply ✓ | 次回クラッシュまで実効性未確認 |
| D1 (etcd defrag CronJob) | boxp/lolice #761 (commit #115db69) | 2026-08-05以前 | ArgoCD 同期済み | 完了 |
| D2 (descheduler 間隔削減) | boxp/lolice #761 (commit #115db69) | 2026-08-05以前 | ArgoCD 同期済み | 完了 |
| D3 (etcd metrics URL) | boxp/arch PR #11944 | 2026-08-05 | 2026-08-17 CI apply ✓ | ⚠️ アクセス制御未実施 (BOXP-177) |

**にもかかわらず 9 時間の無応答が発生した**ことは、watchdog が機能しなかった可能性を示す。
考えられる原因:
1. **電源断またはハードウェア障害** — 電源が物理的に切れると watchdog ハードウェアリセットは発生しない。
2. **I/O障害でjournalバッファ書き込み失敗** — SD カードの I/O が破綻するとjournaldもバッファをdisk flushできず、persistent設定でもクラッシュログが失われる。

### 参考: 適用済み施策の内容 (arch リポジトリ側)

- **[A] systemd hardware watchdog** (実装済み)  
  `RuntimeWatchdogSec=15` + `kernel.hung_task_panic=1` (`hung_task_timeout_secs=300`)  
  効果: フリーズを数分以内に自動再起動に変える。ただし電源断では効かない。

- **[B] armbian-ramlog の無効化 + journal 永続化** (実装済み)  
  `Storage=persistent` + armbian-ramlog ENABLED=false。  
  効果: クラッシュ直前のログが次回起動後に参照可能になる（SDカードI/O完全破綻時を除く）。

- **[D3] etcd `--listen-metrics-urls` 追加** (メトリクスURL設定: 実装済み / アクセス制御: **未実施**)  
  `--listen-metrics-urls=http://0.0.0.0:2381` は boxp/arch PR #11944 で追加済み。  
  **⚠️ セキュリティギャップ**: 現在 2381/TCP は認証なしで全インターフェースに公開されており、
  ホスト側のアクセス制御が未実施。etcd は `hostNetwork: true` のため標準 NetworkPolicy は適用不可。
  アクセス制御の実装（ホスト側 iptables/nftables またはCalico GlobalNetworkPolicy）は BOXP-177 で追跡中。
  実装前に `tcpdump -i any -n port 2381` で実際の Prometheus Pod からの送信元 IP を実測してからルールを設定すること。  
  効果: Prometheus が etcd の WAL fsync latency・backend commit latency を収集し、etcd 書き込み性能の悪化を早期検知できる。

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
- [x] arch リポジトリ: A / B / D3メトリクスURL — boxp/arch PR #11944 (2026-08-05 マージ) で実装済み、2026-08-17 の CI apply で全ノード (shanghai-1/2/3) に適用確認済み
- [x] lolice リポジトリ: D1 / D2 の実装 — commit #115db69 (#761) で実装済み
- [ ] D3 アクセス制御 — 2381/TCP のホスト側フィルタリング未実施 (BOXP-177 で追跡)
- [ ] B 実効性確認 — journal persistent 設定が次回クラッシュ時に実際にログを保持するかの実測未完了
