# BOXP-267: codex-workspace の golyat-3 暫定固定の撤去

## 背景

2026-10-22 に codex-workspace Pod が golyat-4 で `MountVolume.MountDevice ... already mounted or mount point busy` (exit 32) を反復し、owner が #866/#868 で `kubernetes.io/hostname: golyat-3` を nodeSelector に追加して復旧した。

根本原因は golyat-4 の multipathd が Longhorn (tgt) の iSCSI デバイス（vendor `IET` / product `VIRTUAL-DISK`）を取り込み、ブロックデバイスを占有していたこと。恒久対策は boxp/arch PR #13808（`/etc/multipath/conf.d/90-longhorn.conf` で Longhorn デバイスのみ blacklist、worker image にも組込）で、2026-10-22 06:12 UTC に golyat-4 へ限定適用済み。本番 PVC を使わない scratch Longhorn volume で golyat-4 上の「修正前 FailedMount → 修正後 Running/read-write」を確認済み。詳細は Obsidian `Projects/BOXP-267/`。

## 変更

- `argoproj/codex-workspace/deployment.yaml` から今回の暫定固定 `kubernetes.io/hostname: golyat-3` の 1 行だけを削除する。元々の `kubernetes.io/arch: amd64` は維持する。

## merge 時の注意

- Deployment は `Recreate` / replicas=1 のため、Argo CD sync で workspace Pod が一度停止・再作成され、Task Board runner / Codex Cron / Novel Board / Obsidian sync もその間停止する。同 Pod 上で他チケットの run が進行中でない時に merge すること。
- 再作成後の Pod は amd64 ノードのいずれかに配置される。golyat-4 に載った場合はこの PR で修復を検証したことになり、golyat-3 のままなら golyat-4 の修復証明にはならない（証明は scratch volume 検証で別途取得済み）。

## 切戻し

再発時は `kubernetes.io/hostname: golyat-3` を再追加して復旧を優先する。
