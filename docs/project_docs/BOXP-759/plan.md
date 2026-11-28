# BOXP-759 TD-03: 誤削除4runの隔離復元と読み取り照合

## 承認範囲 (Owner, 2026-11-28)
- CD-03 の S3 転送料を、誤削除4run確認のための隔離復元1回に限り承認 (概算 0〜16 USD、超える見込みなら実行前に停止)
- 本番PVC・既存バックアップ・残りの回収候補は変更しない。本番への上書き・追加回収・別課金・権限追加なし

## 転送料の事前見積
- 復元元: backup-4f644d97a4bc40d2 (codex-workspace-home, 2026-11-28T00:00Z, Completed), 非圧縮ブロック合計 160.97GB (status.size)
- Longhorn はブロックを lz4 圧縮して保存。現行FSのファイル実体から 2MiB ブロックを1500点無作為抽出し lz4 圧縮した比率は 0.735
- 転送見込み 約118GB → ap-northeast-1 インターネット送信 0.114 USD/GB で、無料枠なしでも約13.5 USD (無料枠100GBが残っていれば約2 USD)
- 上限 16 USD を超えるのは圧縮比 0.87 超の場合で、見込みとしては範囲内

## 手順
1. `argoproj/codex-workspace/restore-boxp759.yaml` を追加 (fromBackup の一時 StorageClass + 副本1の PVC 150Gi + 読み取り専用マウントの待機 Pod)
2. ArgoCD 同期後、復元完了を確認し、既存 SA の pods/exec で 4run の有無・ファイル一覧・ハッシュを照合 (BOXP-674 は BOXP-743 evidence の uniq 一覧と突合、BOXP-700 は固有ファイル一覧を新規作成)
3. 結果をチケットに報告 (内容は本番PVCへ書き戻さない。テキストの一覧/ハッシュのみ verification-artifacts に残す)
4. 撤去PR (本ファイルと kustomization 参照を削除) → ArgoCD prune → PVC/PV/Longhorn volume の消滅を確認

## Spec-first の適用外
- GitOps マニフェストのみの一時運用作業で、状態機械や実装ロジックの変更がないため Lean/テストの対象外
