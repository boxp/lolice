# BOXP-771: Hermes の保存領域を履歴を消さず緊急拡張する

## 目的
本番 `hermes-agent/hermes-agent-data`（Longhorn `pvc-6178bf56-e149-46d5-8c34-277953d78167`）の容量が逼迫しているため、データを削除せず PVC をオンライン拡張する。Owner の承認済み。

## 拡張前の実測（2026-11-28）
- PVC 要求値: 10Gi。FS: 合計 10,464,022,528 B、使用 9,609,678,848 B、空き 837,566,464 B（92%）
- `KubePersistentVolumeFillingUp` が firing
- 増加量（kubelet_volume_stats_used_bytes）: 30日で +5.3 GB、7日で +2.0 GB（約280 MB/日）、直近1日で +1.1 GB

## 安全確認
- StorageClass `longhorn`: `allowVolumeExpansion: true`
- Longhorn v1.12.2（v1 データエンジン、オンライン拡張に対応）
- ボリュームの状態: attached / healthy、副本 2（golyat-4: ディスク空き 795G、golyat-1: ディスク空き 879G）。over-provisioning は 150%
- 復元点: Longhorn の日次バックアップ（2026-11-28T00:00Z、Completed）

## 容量の選定: 30Gi
- 20Gi の場合、約280 MB/日のペースで 85% に達するまで約1か月しかない
- 30Gi なら空きが約20 GiB になり、満杯まで約70日、85% 到達まで約57日ある。原因調査や整理の時間を取れ、副本 2 つで +40 GiB のスケジュールもディスクの余力内に収まる

## 手順
1. `argoproj/hermes-agent/pvc.yaml` の `storage` を 10Gi から 30Gi に変更（このPR）
2. CI とレビューを通してマージし、ArgoCD の自動 sync で PVC に反映
3. PVC の要求値と容量、Longhorn の `spec.size`、Pod 内の `df` で 30Gi への拡張を確認
4. Hermes の Pod の稼働（会話の再開、通知経路）と、容量警告が解消したことを確認

## 範囲外
削除・prune・VACUUM、他の PVC、codex-workspace、権限や課金の追加
