# BOXP-759: codex-workspace-home PVC を 150Gi → 170Gi に拡張

## 背景
- codex-workspace 作業 PVC (`codex-workspace/codex-workspace-home`) の空きが 15.7GB (91%) まで減り、runner の soft guard (16GiB) を下回って新規着手が待機している。
- 棚卸し (BOXP-759 報告) の結果、即時に安全回収できる量は小さく、Owner 承認により PVC を先行拡張する。

## 変更
- `argoproj/codex-workspace/pvc.yaml` の requests.storage を 150Gi → 170Gi（既存報告の最小案）。

## 事前確認 (2026-11-28 12:4xZ)
- Longhorn volume `pvc-f81b80fe-...`: attached / healthy、replica 2/2 running (golyat-3 usb-ssd, golyat-4 default-disk)。
- 副本ディスク: golyat-3 usb-ssd avail 751GB、golyat-4 avail 861GB。over-provisioning 150% / minimal-available 25% の範囲内で +20Gi スケジュール可能。
- StorageClass `codex-workspace-longhorn` allowVolumeExpansion=true、Longhorn v1.12.2 はオンライン拡張対応。
- ArgoCD app `codex-workspace` は automated (prune/selfHeal) で main を同期。

## やらないこと
- ファイル削除、他 PVC の変更、replica 数・保持ガード閾値の変更、権限追加。

## 確認手順
- PVC capacity 170Gi、Longhorn volume size、`df /home/boxp` のファイルシステム拡張と空き増、runner の容量ガード解除と待機票の着手。

Spec-first/Lean: 構成値の変更のみで状態遷移ルールを伴わないため対象外。
