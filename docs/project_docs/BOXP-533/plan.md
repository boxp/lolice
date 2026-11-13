# BOXP-533: Langfuse の Longhorn replica を 2 にする

## 目的

USB SSD 増設後の Owner 判断に基づき、3 volume の replica を異なるノードに配置し、単一ノード障害に備える。replica はバックアップの代替ではない。

## 現状と容量

| PVC | Longhorn volume | 容量 | 現在の replica |
| --- | --- | --- | --- |
| langfuse/data-langfuse-clickhouse-0 | pvc-4724f3ae-1c78-4cd7-806e-f8e1557bded7 | 20Gi | golyat-3 の root disk `/var/lib/longhorn` に 1 |
| langfuse/data-langfuse-postgres-0 | pvc-dba0ceae-dc34-4ff8-924f-fbd2fc24c8f6 | 5Gi | golyat-1 の root disk `/var/lib/longhorn` に 1 |
| langfuse/data-langfuse-minio-0 | pvc-73aa4c95-7b31-4fde-93cb-22703789c09a | 10Gi | golyat-2 の root disk `/var/lib/longhorn` に 1 |

golyat-1 の `/dev/sdg1` と golyat-3 の `/dev/sdd1` はそれぞれ ext4 983G で、root LV とは別に `/var/lib/longhorn-usb` にマウントされている。Longhorn の usb-ssd disk は Ready / Schedulable で、空き容量はそれぞれ 915G / 763G。追加の必要容量は最大 35Gi。

## 方針

- `langfuse-longhorn` StorageClass を 2 replica・`replicaSoftAntiAffinity: "disabled"`（global の soft anti-affinity=true を継承させず別 node を強制）にし、変更不能な parameters は Argo CD の Force/Replace で StorageClass を再作成して反映する。既存 PV/PVC は維持し、新規 volume に適用する。Delete reclaim と backup 対象外は維持する。
- Longhorn Application の Sync hook Job（filesystem-trim-migration と同型）で既存 3 volume の replica 数を 2、replicaSoftAntiAffinity を disabled にする。対象名を RBAC で限定し、既存 replica を保持したままオンラインで追加・再構築する。PVC 再作成で volume 名が変わる場合は Job を削除または更新する。

## マージ後の確認

Argo CD の sync と Job ログを確認する。各 volume の `numberOfReplicas=2`、異なるノード上の running replica 2 個、robustness healthy、engine が RW であることを確認し、時間を置いて再観察する。他の volume に影響がないこと、および Langfuse の ingest と閲覧を確認する。

## 対象外

データの削除、global setting の変更、バックアップの追加、アプリケーションの HA 化、再起動は行わない。

## ロールバック

必要な場合のみ対象 volume を 1 replica に戻す。replica の削減は冗長性を失いデータ安全性に影響するため、健全性と残す replica の状態を確認し、データ消失リスクを理解した上で明示的に判断して実施する。StorageClass も必要に応じて元に戻す。

## 追補: 初回 merge (#1219) 後の反映不全と修正

- langfuse app: `ServerSideApply=true` に伴う server-side diff の dry-run が、StorageClass.parameters 変更を
  `field is immutable` として拒否し `ComparisonError` で比較自体が失敗、自動同期が起きなかった
  （SC は numberOfReplicas=1 のまま）。BOXP-501 (kohako-studio) と同じく Application に
  `argocd.argoproj.io/compare-options: ServerSideDiff=false` を付け、client-side diff で OutOfSync を検出させ、
  SC 側の `Force=true,Replace=true` で作り直させる。
- longhorn app: 追加物が hook だけだったため OutOfSync にならず自動 sync が走らず、Job が未実行だった。
  ServiceAccount / Role / RoleBinding を通常リソース (wave 0) にし、その差分で自動 sync を起こして wave 1 の Job hook を実行させる。
