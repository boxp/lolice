# T-20260228-006: StarRupture server_files 永続化

## 背景

StarRupture の `DSSettings.txt` がPod再起動時に消失する。
現状は `/home/container/server_files/StarRupture/Saved/SaveGames` のみが PVC で永続化されており、
`server_files` ディレクトリ全体（DSSettings.txt, ゲーム設定等）は永続化されていない。

## 変更内容

### 1. 新規 PVC 作成
- `starrupture-server-files-claim` (10Gi, longhorn, RWO)
- `/home/container/server_files` 全体をカバー

### 2. Deployment 修正
- volumeMount を `/home/container/server_files` に変更（旧: SaveGames サブディレクトリのみ）
- volume 名を `server-files-volume` に変更
- `USE_DSSETTINGS` 環境変数を `true` に変更（DSSettings.txt の利用を有効化）

### 3. 旧 PVC の保持
- `starrupture-saved-claim` はデータ移行のために残存
- 移行完了後に別PRで削除予定

### 4. ドキュメント
- `docs/project_docs/T-20260228-006/runbook.md` に移行手順・ロールバック方法を記載

## リスクと対策

| リスク | 対策 |
|--------|------|
| 既存 SaveGames データが新PVCに含まれない | 移行手順を runbook に明記。旧PVCを保持 |
| server_files ディレクトリの初回起動時が空 | ゲームサーバーが初回起動時に自動生成する |
| 容量不足 | 10Gi に拡大（旧5Gi） |

## ロールバック

1. deployment.yaml の volumeMount を旧 SaveGames パスに戻す
2. PVC 参照を `starrupture-saved-claim` に戻す
3. `USE_DSSETTINGS` を `false` に戻す
