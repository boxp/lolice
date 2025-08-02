# Longhorn S3バックアップ設定

## 概要

LonghornでAWS S3をバックアップ先として使用し、特にark serverのPVCをバックアップできるように設定を行う。

## 背景

- ark-survival-ascendedアプリケーションのPVC `ark-server-data-claim` (10Gi) の永続データを保護する必要がある
- Longhornの分散ストレージシステムに加えて、外部バックアップによる冗長性を確保したい
- AWS S3を利用してコスト効率的なバックアップストレージを実現する

## 設計

### バックアップ先設定

- **S3バケット**: `longhorn-backup`
- **リージョン**: `ap-northeast-1` (東京)
- **バックアップターゲット**: `s3://longhorn-backup@ap-northeast-1/`

### 認証情報管理

External Secrets Operatorを使用してAWS認証情報を安全に管理:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: longhorn-backup-secret
  namespace: longhorn-system
spec:
  secretStoreRef:
    name: parameterstore
    kind: ClusterSecretStore
  target:
    name: longhorn-backup-secret
  data:
  - secretKey: AWS_ACCESS_KEY_ID
    remoteRef:
      key: /longhorn/backup/aws-access-key-id
  - secretKey: AWS_SECRET_ACCESS_KEY
    remoteRef:
      key: /longhorn/backup/aws-secret-access-key
  - secretKey: AWS_ENDPOINTS
    remoteRef:
      key: /longhorn/backup/aws-endpoints
```

### Longhorn設定

以下のSettingリソースを追加:

1. **backup-target**: S3バケットの指定
2. **backup-target-credential-secret**: 認証情報Secretの指定

```yaml
apiVersion: longhorn.io/v1beta1
kind: Setting
metadata:
  name: backup-target
  namespace: longhorn-system
value: "s3://longhorn-backup@ap-northeast-1/"
---
apiVersion: longhorn.io/v1beta1
kind: Setting
metadata:
  name: backup-target-credential-secret
  namespace: longhorn-system
value: "longhorn-backup-secret"
```

## 実装手順

### 1. AWS側の準備

1. S3バケット `longhorn-backup` を東京リージョン(ap-northeast-1)に作成
2. IAMユーザーまたはロールでS3アクセス権限を設定
3. AWS SSM Parameter Storeに認証情報を格納:
   - `/longhorn/backup/aws-access-key-id`
   - `/longhorn/backup/aws-secret-access-key`
   - `/longhorn/backup/aws-endpoints` (オプション)

### 2. Kubernetes側の設定

1. `argoproj/longhorn/settings.yaml` にバックアップ設定を追加
2. `argoproj/longhorn/external-secret.yaml` にS3認証情報のExternal Secretを追加
3. ArgoCDで自動同期またはManual Syncを実行

### 3. バックアップの実行

- **手動バックアップ**: Longhorn UIまたはkubectlでVolume Backupを作成
- **自動バックアップ**: RecurringJobリソースでスケジュール設定可能

## セキュリティ考慮事項

- AWS認証情報はExternal Secrets Operatorで管理し、平文でコミットしない
- S3バケットへのアクセスは最小権限の原則に従う
- バックアップデータの暗号化を検討（S3サーバーサイド暗号化）

## 運用

### バックアップの確認

```bash
# Longhornバックアップ一覧確認
kubectl get backups -n longhorn-system

# 特定のVolumeのバックアップ確認
kubectl describe volume <volume-name> -n longhorn-system
```

### リストア

1. Longhorn UIでBackupからVolumeを復元
2. 新しいPVCを作成してアプリケーションにアタッチ

## 対象リソース

- **Primary**: `ark-server-data-claim` (ark-survival-ascended namespace)
- **Future**: 他の重要なPVCにも同様の設定を適用可能

## 参考資料

- [Longhorn Backup Documentation](https://longhorn.io/docs/latest/snapshots-and-backups/backup-and-restore/set-backup-target/)
- [AWS S3 Documentation](https://docs.aws.amazon.com/s3/)