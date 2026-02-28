# StarRupture server_files 永続化 — Runbook

## 概要

`/home/container/server_files` を PVC `starrupture-server-files-claim` で永続化する。
これにより `DSSettings.txt` を含む全サーバーファイルがPod再起動後も保持される。

## 永続化対象

| パス | 内容 |
|------|------|
| `/home/container/server_files/DSSettings.txt` | サーバー設定ファイル（ポート、パスワード等） |
| `/home/container/server_files/StarRupture/Saved/SaveGames/` | セーブデータ |
| `/home/container/server_files/StarRupture/Saved/Config/` | ゲーム設定 |
| `/home/container/server_files/StarRupture/Saved/Logs/` | ログファイル |

## 初回デプロイ手順

### 1. ArgoCD 同期前の準備

マニフェストがmainにマージされると ArgoCD が自動同期する（selfHeal: true）。
新PVCが作成され、Deploymentが更新される。

**注意**: 新PVCは空の状態で作成されるため、初回起動時にゲームサーバーが必要なファイルを自動生成する。

### 2. 既存 SaveGames データの移行

旧PVC `starrupture-saved-claim` から新PVC `starrupture-server-files-claim` へデータを移行する。

```bash
# 1. Deployment を 0 にスケールダウン
kubectl -n starrupture scale deployment starrupture --replicas=0

# 2. 一時的なデータ移行用 Pod を作成
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: starrupture-data-migration
  namespace: starrupture
spec:
  containers:
  - name: migration
    image: busybox
    command: ["sleep", "3600"]
    volumeMounts:
    - name: old-volume
      mountPath: /old-data
    - name: new-volume
      mountPath: /new-data
  volumes:
  - name: old-volume
    persistentVolumeClaim:
      claimName: starrupture-saved-claim
  - name: new-volume
    persistentVolumeClaim:
      claimName: starrupture-server-files-claim
  restartPolicy: Never
EOF

# 3. Pod が Ready になるまで待機
kubectl -n starrupture wait --for=condition=Ready pod/starrupture-data-migration --timeout=120s

# 4. SaveGames データをコピー
kubectl -n starrupture exec starrupture-data-migration -- \
  mkdir -p /new-data/StarRupture/Saved/SaveGames
kubectl -n starrupture exec starrupture-data-migration -- \
  cp -a /old-data/. /new-data/StarRupture/Saved/SaveGames/

# 5. コピーされたデータを確認
kubectl -n starrupture exec starrupture-data-migration -- \
  ls -la /new-data/StarRupture/Saved/SaveGames/

# 6. 移行用 Pod を削除
kubectl -n starrupture delete pod starrupture-data-migration

# 7. Deployment を 1 にスケールアップ
kubectl -n starrupture scale deployment starrupture --replicas=1
```

### 3. 動作確認

```bash
# Pod が正常に起動しているか確認
kubectl -n starrupture get pods

# DSSettings.txt が存在するか確認
kubectl -n starrupture exec deploy/starrupture -- ls -la /home/container/server_files/DSSettings.txt

# SaveGames が移行されているか確認
kubectl -n starrupture exec deploy/starrupture -- ls -la /home/container/server_files/StarRupture/Saved/SaveGames/
```

## 容量

- 新PVC: 10Gi（旧5Giから拡大）
- StorageClass: longhorn (動的プロビジョニング)
- 必要に応じて `kubectl edit pvc` で拡張可能（longhornは拡張対応）

## ロールバック手順

問題が発生した場合の復旧手順:

1. `deployment.yaml` を以前のコミットに戻す:
   ```bash
   git revert <commit-hash>
   ```
2. ArgoCD が自動同期し、旧構成に戻る
3. 旧PVC `starrupture-saved-claim` のデータは保持されているため、SaveGamesは復旧される

**注意**: ロールバック後、新PVC上に保存されたDSSettings.txt等は使われなくなるが、データ自体はPVC上に残る。

## 旧PVC削除のタイミング

以下を全て確認した後、別PRで `starrupture-saved-claim` を削除する:

1. 新PVCでサーバーが正常に動作している
2. SaveGames データが正常に移行されている
3. 少なくとも1週間の運用実績がある
