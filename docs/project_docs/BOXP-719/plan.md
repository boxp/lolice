# BOXP-719: Longhorn バックアップ置き場の確認間隔 5 分 → 1 時間

## 目的
BOXP-598 §14 の案 C（Owner 承認済み）。S3 Tier1 リクエスト（毎日 5〜6 万件の下限）のうちバックアップ置き場確認分を減らす。月最大約 7 USD 減は未実証の推計。

## 変更
- `argoproj/longhorn/application.yaml` の Helm values に `defaultBackupStore.pollInterval: 3600` を追加。
  - chart 1.12.2 では `longhorn-default-resource` ConfigMap の `backupstore-poll-interval` になり、
    longhorn-manager (`CreateOrUpdateDefaultBackupTarget`) が既存 default BackupTarget の `spec.pollInterval` を更新する。
  - URL / 認証 Secret は ConfigMap に無ければ既存値を引き継ぐので指定しない。

## 変えないもの
バックアップ頻度・保持・復元対象・RecurringJob、B（領域分離）、D（旧版保持短縮）、バックアップ削除、IAM。

## 確認
1. Argo CD 自動 sync 後、`kubectl get backuptarget default -n longhorn-system` で `pollInterval: 1h0m0s`、`status.available: true`。
2. 次回の日次バックアップが Completed になり一覧に出ること。
3. 既存の日次 S3 使用タイプ内訳 (`aws_cost_s3_usage_type_*`) で、変更後 1 週間の Tier1 下限日（変更前 5.2 万件）を比較。

## 戻し方
`defaultBackupStore` ブロックを削除し、`pollInterval: 300` を一時指定して戻す（ConfigMap から消すだけでは既存値が残るため）。

## 仕様先行・Lean について
設定値 1 つの変更で状態機械の規則がないため、spec/Lean は適用しない。
