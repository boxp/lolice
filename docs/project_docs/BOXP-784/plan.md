# BOXP-784: golyat-3 内蔵ディスクの Longhorn 副本を USB SSD へ安全に移す

## 背景
- golyat-3 `default-disk-a3398d1eaba7a55e` が DiskPressure で Schedulable=False (LonghornDiskNotSchedulable firing)。
  storageAvailable ≈59.9GB < MinimalAvailable(25%) ≈61.9GB。
- 同ノード `usb-ssd` は空き ≈733GB / Schedulable=True。
- 内蔵ディスク上の副本は 4 本のみ (いずれも healthy):
  - pvc-948d20d3-2c85-4fb2-92c1-a058fe44912c (etcd-snapshots, 2 副本)
  - pvc-cad21c5b-f44a-41e6-b7a2-3add70976c11 (kohako-studio-tts-data, 3 副本)
  - pvc-9d4507a5-ad0f-47d4-9317-87b8a0c489f9 (grafana, 2 副本)
  - pvc-2c18da1d-1286-4a8e-ad21-d5a5e9833efb (kohako-studio-data, 3 副本)
- BOXP-759 の復元用 volume pvc-439ec09c は対象外 (権限も与えない)。

## Owner 承認 (選択 B)
codex-workspace SA に golyat-3 node CR と上記 4 volume CR だけの patch 権限を、正規 GitOps で作業中のみ付与。

## 手順
1. 本 PR: `argoproj/codex-workspace/rbac-boxp784-temp.yaml` で resourceNames 限定 ClusterRole/ClusterRoleBinding (kustomize namespace 上書き回避のため cluster-scoped。対象 CR は longhorn-system のみに存在) を追加。
2. 適用後 `kubectl auth can-i` で対象内は yes、対象外 (他 node / 復元 volume / 他 verb) は no を照合。
3. golyat-3 usb-ssd に一時タグ `boxp784-target` を追加。
4. 4 volume に `spec.diskSelector: [boxp784-target]` を設定 (既存副本は再配置されない)。
5. default-disk を `allowScheduling=false, evictionRequested=true` (Longhorn 正規のディスク退避: 新副本 healthy 後に旧副本削除)。
6. 新副本が golyat-3 usb-ssd で running、volume healthy、旧副本消滅を確認。
7. 一時設定を戻す: evictionRequested=false / allowScheduling=true / diskSelector=[] / タグ=[bulk, usb-ssd]。
8. 撤去 PR で本 Role/RoleBinding を削除し、ArgoCD prune 後に can-i=no を読戻す。
9. 事後確認: 両ディスクの空き・副本構成・Schedulable 復帰・警告解消・BOXP-759 復元への影響なし。

## 対象外
閾値/予約の変更、他ノード設定変更、データ手動削除、コンテナ領域清掃、警告抑制。

## 仕様先行開発について
権限設定 (RBAC マニフェスト) と運用手順のみで、状態機械として検証すべき新規ロジックがないため Lean/テスト先行は適用しない。

## 実施結果 (2026-11-28)
- 17:0xZ can-i 照合: golyat-3 node / 対象4volume の patch=yes、golyat-1・復元volume pvc-439ec09c・delete/update・replicas=no。
- 17:04Z 退避開始 → 17:05:39Z 旧副本4本消滅。新副本は全て golyat-3 usb-ssd (diskID 6d9037dd) で running、4volume とも attached/healthy、副本数 (2/3/2/3) 維持。
- 一時設定を戻し、golyat-3 node spec が事前スナップショットと完全一致することを diff で確認。
- default-disk: 空き 59.87GB→64.59GB、Schedulable False(DiskPressure)→True。usb-ssd: 空き 728.7GB→723.9GB。
- LonghornDiskNotSchedulable (default-disk-a3398d1eaba7a55e) は 17:05:40Z を最後に解消。
- BOXP-759 復元 volume は副本 golyat-1 のまま、復元進行 73%→84% で継続。
- 本 PR で一時 ClusterRole/ClusterRoleBinding を撤去する。
