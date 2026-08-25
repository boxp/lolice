# BOXP-176: shanghai-3 到達不能 — 原因調査と再発防止 (INC-5 post-incident)

## 概要

2026-08-19 02:10 UTC 頃、`shanghai-3` (192.168.10.104 / Orange Pi Zero 3 / control-plane) が
約 9 時間にわたって到達不能となった。物理再起動で復旧し、SD カード交換は不要だった。

本ドキュメントは BOXP-176 の調査結果と再発防止策のまとめ。
インシデントチケットは Obsidian vault の `Incidents/Tickets/INC-5.md` を参照。

## 調査結果（初期調査: 2026-08-19）

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

### 初期現地調査結果 (再起動後)

- SD カード: SanDisk SA32G 29.1 GiB (manfid 0x000002)。現 boot で I/O エラーなし。
- `dmesg` および `journalctl -k` での I/O エラー確認: 現 boot では `mmcblk0` 関連の I/O エラーなし。
- `life_time` sysfs: SDHC カードは非対応のため取得不可。
- 前回 boot の journal: **消失**（後日の実効性確認で原因判明 → 後述）。
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

---

## フォローアップ調査（2026-08-25 実施）

INC-5 後に「再発防止策 A・B が本当に機能しているか」を全 3 control-plane で実測確認した。
**以下の重大な事実が判明し、施策の実効性評価を修正する。**

### journal 永続化の実効性確認

**結論: journal 永続化は機能していない（全 3 ノード）。**

| ノード | journald.conf Storage | /var/log/journal/ | 表示 boot 数 | 前回 boot ログ |
|---|---|---|---|---|
| shanghai-1 | `volatile` | 存在（ディレクトリのみ） | 1 boot（現 boot: 2026-08-24 02:42 UTC） | **なし** |
| shanghai-2 | `volatile` | 存在（ディレクトリのみ） | 2 boot（前回: 8/15-8/23, 現在: 8/25） | **あり**（理由不明 ※1） |
| shanghai-3 | `volatile` | 存在（ディレクトリのみ） | 1 boot（現 boot: 2026-08-19 16:03 UTC） | **なし** |

※1: `Storage=volatile` ではジャーナルは tmpfs (`/run/log/journal/`) にのみ書かれ、正常シャットダウンでも `/var/log/journal/` には永続化されない。shanghai-2 に前回 boot ログが存在する理由は現状不明（過去に `Storage=persistent` が設定されていた際のジャーナルバイナリが `/var/log/journal/` に残留している可能性、または測定時点の特殊な状態）。本番への影響を評価するため、実際に `reboot` 後も前回 boot ログが読めるかを確認する必要がある（可逆操作として次回メンテナンス時に検証を推奨）。

**原因**: `/etc/systemd/journald.conf` の `Storage=volatile` が適用されたまま。
`Storage=volatile` のとき journald は tmpfs (`/run/log/journal/`) にログを書く。
`/var/log/journal/` ディレクトリが存在しても `Storage=volatile` 優先で RAM ストレージが使われ、
ハードリブート（電源断・ハングからの強制再起動）時には RAM 上の未フラッシュログが全消失する。

`armbian-ramlog` の `ENABLED=false` は確認済み（正常）。しかし journald の `Storage` 設定が
`volatile` のまま残っているため、ramlog 無効化の効果が活きていない。

**影響**: 次回ハング発生時も前回 boot のログが失われ、根本原因調査が不可能なままとなる。

### RuntimeWatchdog の実効性確認

**結論: RuntimeWatchdog の実効状態は未確認（直接確認未実施）。**

| 確認項目 | shanghai-1 | shanghai-2 | shanghai-3 |
|---|---|---|---|
| `/etc/systemd/system.conf` RuntimeWatchdogSec | `#RuntimeWatchdogSec=off`（コメントアウト） | 同左 | 同左 |
| `/etc/systemd/system.conf.d/*.conf` drop-in | 未確認 | 未確認 | 未確認 |
| `systemctl show --property=RuntimeWatchdogUSec` | 未実施 | 未実施 | 未実施 |
| `/dev/watchdog` デバイス | 未確認 | 未確認 | 未確認 |

`/etc/systemd/system.conf` 本体の `RuntimeWatchdogSec` はコメントアウト状態だが、
**これだけでは「未動作」と断定できない**。systemd は drop-in ディレクトリ
(`/etc/systemd/system.conf.d/*.conf`) の設定を本体より優先して読み込むため、
drop-in ファイルで `RuntimeWatchdogSec=15` が設定されている場合は watchdog が有効になる。
また、**実際の systemd の watchdog 状態を確認するには `systemctl show --property=RuntimeWatchdogUSec`
を実行する必要がある**（このコマンドは未実施）。
なお kubelet の `WatchdogUSec` は kubelet 自身のウォッチドッグ設定であり、
systemd PID 1 が管理するハードウェア watchdog デバイスの状態とは無関係である。

以上より RuntimeWatchdog の実効状態は**未確認**とする。
次回メンテナンス時に SSH 接続が可能な状態で以下を確認すること:
```
systemctl show --property=RuntimeWatchdogUSec
ls /etc/systemd/system.conf.d/
ls /dev/watchdog*
```

**影響**: RuntimeWatchdog が実際に無効であれば、カーネルハング発生時の自動再起動機能が期待できず、
物理介入なしの復旧が困難となる可能性がある。ただし INC-5 での 9 時間無応答との因果関係は
直接検証なしに断定できない。

### etcd WAL fsync 遅延・ディスク I/O の実測値（2026-08-25 時点）

etcd メトリクスポート（2381）および node_exporter（9101）から採取。
以下の値は各ノードの現 boot 開始からの**累積ヒストグラム**で、過去スパイクを含む。

#### etcd WAL fsync latency (p99)

| ノード | p50 | p90 | p99 | 評価 |
|---|---|---|---|---|
| shanghai-1 | 3.6 ms | 11.3 ms | **84.5 ms** | ⚠️ 要注意（etcd 推奨 < 10 ms） |
| shanghai-2 | 1.8 ms | 5.1 ms | 10.8 ms | 正常範囲 |
| shanghai-3 | 1.8 ms | 5.6 ms | 13.0 ms | 正常範囲 |

#### etcd backend commit latency (p99)

| ノード | p50 | p90 | p99 | 評価 |
|---|---|---|---|---|
| shanghai-1 | 12.0 ms | 20.4 ms | **165.5 ms** | ⚠️ 高値（過去スパイクを累積） |
| shanghai-2 | 5.8 ms | 7.8 ms | 16.0 ms | 正常範囲 |
| shanghai-3 | 5.8 ms | 7.9 ms | 21.4 ms | 正常範囲 |

#### mmcblk0 write latency（node_exporter, 現 boot 累積）

| ノード | 平均書き込みレイテンシ | I/O weighted latency | ディスク利用率 |
|---|---|---|---|
| shanghai-1 | 8.42 ms | 9.32 ms（稼働 32.8h） | 13.4% |
| shanghai-2 | 2.61 ms | 2.66 ms（稼働 22.1h） | 5.4% |
| shanghai-3 | 2.74 ms | 3.30 ms（稼働 5.6d） | 5.6% |

**考察**: shanghai-1 の書き込みレイテンシが他の 2 ノードの約 3 倍。
INC-2（2026-08-01 shanghai-1 ハング）直前にも write await 劣化が記録されており、
shanghai-1 の eMMC が継続的に I/O 性能低下状態にある可能性がある。
shanghai-3 は 5.6 日間で書き込み回数が非常に多く（27M writes）、累積 write amplification が大きい。

---

## 再発防止策の状況（2026-08-25 時点の実効性評価）

**⚠️ 注意: 以下は 2026-08-25 の実測調査を反映した修正済み評価。**
boxp/arch PR #11944 (2026-08-05) および 2026-08-17 CI apply は完了しているが、
実ノードで設定が意図通りに効いているかは別途確認が必要。

| 施策 | Ansible/PR | ノード適用 | 実効性確認 | 備考 |
|---|---|---|---|---|
| A (watchdog) | boxp/arch PR #11944 | 2026-08-17 CI ✓ | ❓ 未確認（system.conf 本体はコメントアウト、drop-in や systemctl show 未確認） | 要確認 |
| B (journal永続化) | boxp/arch PR #11944 | 2026-08-17 CI ✓ | ❌ 未動作（Storage=volatile のまま） | 要修正 |
| D1 (etcd defrag) | lolice #761 | ArgoCD 同期済み | ✅ 実装済み | 完了 |
| D2 (descheduler 間隔) | lolice #761 | ArgoCD 同期済み | ✅ 実装済み | 完了 |
| D3 (etcd metrics URL) | boxp/arch PR #11944 | 2026-08-17 CI ✓ | ✅ ポート 2381 応答確認 | ⚠️ アクセス制御未実施 (BOXP-177) |

### A・B が未動作であることの影響

Ansible での設定変更が実際のノード設定に反映されていない原因として、以下が考えられる（要調査）:
- Ansible task が idempotent に動作せず特定の条件分岐で skip された
- 設定ファイルのパスまたはセクションが想定と異なる（例: `/etc/systemd/system.conf` vs `/etc/systemd/system.conf.d/`）
- CI apply のログが「changed」でなく「ok」だったため問題が見過ごされた

**現在の状態**: 全 3 control-plane は watchdog 設定未適用（推定）・journal 非永続でハング耐性が実質的に未改善のまま稼働中。

### 参考: 施策の意図した動作

- **[A] systemd hardware watchdog** (`RuntimeWatchdogSec=15`)
  意図: カーネルハング検知後 ~15 秒以内にハードウェアリセットをかける。
  現状: `/etc/systemd/system.conf` 本体の `RuntimeWatchdogSec` はコメントアウト状態。ただし drop-in (`/etc/systemd/system.conf.d/*.conf`) が存在する場合は上書きされる。`systemctl show --property=RuntimeWatchdogUSec` による実際の有効値確認および `/dev/watchdog` デバイスの確認は未実施のため、実効状態は**未確認**。

- **[B] journal 永続化** (`Storage=persistent`)
  意図: クラッシュ直前のログを次回起動後に参照できるようにする（SD I/O 完全破綻時は失敗する場合あり）。
  現状: `Storage=volatile` のため RAM にのみ保存。ハード再起動で全消失。

- **[D3] etcd metrics** (`--listen-metrics-urls=http://0.0.0.0:2381`)
  意図: Prometheus が etcd の WAL fsync latency・backend commit latency を収集し I/O 劣化を早期検知。
  現状: メトリクス URL は動作中。ただし認証なし全インターフェース公開状態（BOXP-177 で追跡）。
  `mmcblk0` write await は node_exporter (9101) の別指標が担当。

### 長期恒久案

- **[C] etcd データディレクトリを USB SSD / eMMC 以外の耐久媒体へ移行**
  `/var/lib/etcd` を microSD から外す。本命の恒久対策。
  必要な手順: etcd snapshot → member remove → 新ストレージで rejoin（master 承認必須）。
  rollback: 変更前 snapshot と旧 member 設定で元に戻す。

- **[E] control-plane 障害ドメイン分離の改善**
  現状の 3 node はすべて同一 eMMC 系統に依存。etcd quorum が 2/3 で維持される間は可用性あるが、
  2 ノード同時障害時に quorum を失う。外部 etcd クラスター化や専用ストレージ基盤の検討。

---

## 監視・アラート・Runbook の設計

### 現在の監視状況

- `node_disk_write_time_seconds_total / node_disk_writes_completed_total` (mmcblk0) を Prometheus で収集済み（平均書き込みレイテンシ。累積カウンタの除算のため p99 は算出不可）。
- etcd メトリクス (`etcd_disk_wal_fsync_duration_seconds_bucket` 等) は D3 で収集中。
- Kubernetes Node Ready/NotReady は kube-state-metrics 経由で収集済み。
- Alertmanager でメール通知設定済み（PR #763 で実装）。

### 実装済みアラートルール（`argoproj/prometheus-operator/control-plane-node-rules.yaml`）

| アラート名 | 条件 | 重要度 |
|---|---|---|
| ControlPlaneNodeNotReady | control-plane Node の Ready 条件が true 以外 (5 分以上) | critical |
| EtcdMemberDown | etcd member 数 < 2 (2 分以上) | critical |
| EtcdHighFsyncDuration | etcd WAL fsync p99 > 500 ms (10 分継続) | warning |
| ControlPlaneFilesystemReadOnly | rootfs / var 等が read-only (1 分以上) | critical |
| ControlPlaneHighDiskIOWait | mmcblk0 I/O busy 率 > 90% (15 分継続) | warning |

### 不足・要改善のアラート（後続チケット化予定）

| アラート名 | 現状の問題 | 推奨改善内容 |
|---|---|---|
| EtcdHighFsyncDuration の閾値 | 現行 p99 > 500ms は重篤な状態のみ検知。INC-5 時点の shanghai-1 p99=84ms は通知されない | 早期検知のため p99 > 50ms (warning) / > 200ms (critical) への引き下げを検討 |
| mmcblk0 書き込みレイテンシ | 現行は I/O busy 率 (> 90%) のみ。平均書き込みレイテンシ高値の検知がない | 平均書き込みレイテンシ (`node_disk_write_time_seconds_total / node_disk_writes_completed_total`) の閾値アラート追加を検討（p99 はヒストグラム非収集のため算出不可） |

### 検知から復旧までの時間目標

| フェーズ | 手段 | 目標時間 |
|---|---|---|
| 検知 | アラートメール受信 | < 5 分 |
| 一次確認 | kubectl get nodes / etcd endpoint health | < 10 分 |
| 自動復旧（watchdog 動作時） | RuntimeWatchdog によるハードリセット | < 1 分（ハング後） |
| 手動確認・物理介入 | 電源/LAN/console 確認 | < 60 分（現地担当者） |
| etcd snapshot 退避 | 生存 node から退避・別媒体に保存 | < 30 分 |
| etcd quorum 維持確認 | 2/3 以上が healthy であること | 継続監視 |

### 物理対応 Runbook

現在の runbook: `Incidents/Runbooks/shanghai-control-plane-sdcard-failure.md`

物理確認チェックリスト（ご主人さまの現地対応用）:
1. 電源 LED・給電状態・電源ケーブルの目視確認
2. Ethernet link LED・LAN ケーブル・スイッチポートの確認
3. シリアル console (利用可能な場合) で boot/rootfs mount/mmc I/O エラーを採取
4. SDカードの物理破損・接触不良の目視確認（抜き差し・交換は承認後のみ）
5. 物理 reboot の前に生存 node から etcd snapshot を別媒体に退避

---

## 現在のアクション状況

- [x] INC-5 インシデントチケット作成 (`Incidents/Tickets/INC-5.md`)
- [x] Incident Board 更新 (Monitoring レーンに INC-5 追加)
- [x] Runbook 更新 (`Incidents/Runbooks/shanghai-control-plane-sdcard-failure.md`)
- [x] `docs/project_docs/shanghai-node-resilience/plan.md` に INC-5 の再発を記録
- [x] D1 (etcd defrag CronJob) — lolice #761 実装済み
- [x] D2 (descheduler 間隔削減) — lolice #761 実装済み
- [x] D3 メトリクス URL — boxp/arch PR #11944 で設定済み、2381 ポート応答確認済み
- [ ] **A・B の実効性修正** — boxp/arch 側で `RuntimeWatchdogSec=15` と `Storage=persistent` が実際に適用されるよう修正（後続チケット必須）
- [ ] D3 アクセス制御 — 2381/TCP のホスト側フィルタリング未実施 (BOXP-177 で追跡)
- [ ] etcd/mmcblk0 アラートルール追加 — 上記「推奨アラート設計」を PrometheusRule に実装
- [ ] C (etcd を USB SSD 等へ移行) — 長期対策、ご主人さまの承認と停止計画が必要
