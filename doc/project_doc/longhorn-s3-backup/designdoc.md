# Longhorn S3バックアップ設定

## 概要

LonghornでAWS S3をバックアップ先として使用し、特にark serverのPVCをバックアップできるように設定を行う。

## 背景

- ark-survival-ascendedアプリケーションのPVC `ark-server-data-claim` (10Gi) の永続データを保護する必要がある
- Longhornの分散ストレージシステムに加えて、外部バックアップによる冗長性を確保したい
- AWS S3を利用してコスト効率的なバックアップストレージを実現する

## 設計

### バックアップ先設定

- **S3バケット**: `boxp-longhorn-backup`
- **リージョン**: `ap-northeast-1` (東京)
- **バックアップターゲット**: `s3://boxp-longhorn-backup@ap-northeast-1/`

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
value: "s3://boxp-longhorn-backup@ap-northeast-1/"
---
apiVersion: longhorn.io/v1beta1
kind: Setting
metadata:
  name: backup-target-credential-secret
  namespace: longhorn-system
value: "longhorn-backup-secret"
```

## 実装手順

### 1. AWS側の準備 (boxp/archプロジェクトでの対応)

boxp/archプロジェクトのディレクトリ構造に基づき、新しいTerraformコンポーネント `terraform/aws/longhorn/` を作成する必要があります。

#### 1.1 新しいTerraformディレクトリの作成

boxp/archプロジェクトでは、新しいワーキングディレクトリの作成にTFActionのscaffold機能を使用する必要があります。

**GitHub Actionsを使用したscaffold**:

1. boxp/archリポジトリのGitHub Actionsページにアクセス
2. "Scaffold a working directory" ワークフローを手動実行
3. `working_dir` パラメータに `terraform/aws/longhorn` を指定
4. ワークフローが自動的にPRを作成し、必要なファイルを生成

このワークフローは以下を自動実行します：
- `templates/aws/` から基本ファイルをコピー
- `tfaction.yaml` 設定ファイルを作成
- `aqua` 設定の初期化
- 適切なbackend.tf設定の生成

**手動での確認事項**:
scaffoldが完了したら、以下の設定が正しく生成されていることを確認：

```bash
# 生成されるディレクトリ構造
terraform/aws/longhorn/
├── backend.tf        # S3バックエンド設定
├── provider.tf       # AWSプロバイダー設定
├── tfaction.yaml     # TFAction設定
└── aqua/             # ツール依存関係設定
    ├── aqua-checksums.json
    ├── aqua.yaml
    └── imports/
        ├── terraform.yaml
        ├── tflint.yaml
        └── trivy.yaml
```

#### 1.2 S3バケットの作成

`terraform/aws/longhorn/s3.tf` を作成:

```hcl
# S3 bucket for Longhorn backups
resource "aws_s3_bucket" "longhorn_backup" {
  bucket = "boxp-longhorn-backup"
}

# Block all public access to the bucket
resource "aws_s3_bucket_public_access_block" "longhorn_backup" {
  bucket = aws_s3_bucket.longhorn_backup.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "longhorn_backup" {
  bucket = aws_s3_bucket.longhorn_backup.id
  
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "longhorn_backup" {
  bucket = aws_s3_bucket.longhorn_backup.id
  
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "longhorn_backup" {
  bucket = aws_s3_bucket.longhorn_backup.id
  
  rule {
    id     = "delete-old-backups"
    status = "Enabled"
    
    expiration {
      days = 30
    }
    
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}
```

#### 1.3 IAMユーザーとポリシーの作成

`terraform/aws/longhorn/iam.tf` を作成:

```hcl
# IAM User for Longhorn S3 backup access
resource "aws_iam_user" "longhorn_backup" {
  name = "longhorn-backup-user"
  path = "/system/"
  
  tags = {
    Description = "User for Longhorn S3 backup access"
    Project     = "lolice"
  }
}

resource "aws_iam_user_policy" "longhorn_backup" {
  name = "longhorn-backup-policy"
  user = aws_iam_user.longhorn_backup.name
  
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = aws_s3_bucket.longhorn_backup.arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = "${aws_s3_bucket.longhorn_backup.arn}/*"
      }
    ]
  })
}

resource "aws_iam_access_key" "longhorn_backup" {
  user = aws_iam_user.longhorn_backup.name
}
```

#### 1.4 SSM Parameter Storeへの認証情報格納

`terraform/aws/longhorn/ssm.tf` を作成:

```hcl
# SSM Parameters for Longhorn backup credentials
resource "aws_ssm_parameter" "longhorn_backup_access_key" {
  name        = "/longhorn/backup/aws-access-key-id"
  description = "AWS Access Key ID for Longhorn backup"
  type        = "SecureString"
  value       = aws_iam_access_key.longhorn_backup.id
  
  tags = {
    Description = "Longhorn backup user access key ID"
    Project     = "lolice"
  }
}

resource "aws_ssm_parameter" "longhorn_backup_secret_key" {
  name        = "/longhorn/backup/aws-secret-access-key"
  description = "AWS Secret Access Key for Longhorn backup"
  type        = "SecureString"
  value       = aws_iam_access_key.longhorn_backup.secret
  
  tags = {
    Description = "Longhorn backup user secret access key"
    Project     = "lolice"
  }
}

resource "aws_ssm_parameter" "longhorn_backup_endpoints" {
  name        = "/longhorn/backup/aws-endpoints"
  description = "AWS endpoints for Longhorn backup (optional)"
  type        = "String"
  value       = ""  # デフォルトのAWSエンドポイントを使用
  
  tags = {
    Description = "Longhorn backup AWS endpoints (optional)"
    Project     = "lolice"
  }
}
```

#### 1.5 TFAction設定の確認

scaffold実行後、`terraform/aws/longhorn/tfaction.yaml` が以下のような内容で自動生成されることを確認:

```yaml
# scaffold により自動生成される設定
terraform_plan_config:
  - target: terraform/aws/longhorn
    tfmigrate_plan_config:
      enabled: true

terraform_apply_config:
  - target: terraform/aws/longhorn
```

この設定により、AWS IAMロールと適切なアクセス権限が自動的に設定されます（`tfaction-root.yaml`の`target_groups`で定義済み）。

#### 1.7 External Secrets Operatorの権限確認

既存のExternal Secrets Operatorの設定（`terraform/aws/external-secrets-operator/iam.tf`）では、すでにワイルドカードでSSMパラメータへのアクセスが許可されているため、追加の権限設定は不要です：

```hcl
# 既存の設定ですでに以下の権限が付与されている
Resource = [
  "*",
]
```

これにより、新しく作成される `/longhorn/backup/*` パラメータも自動的にアクセス可能になります。

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

## boxp/archでの実装手順まとめ

1. **TFActionのscaffold機能を使用したディレクトリ作成**
   - GitHub Actionsの "Scaffold a working directory" ワークフローを実行
   - `terraform/aws/longhorn` を指定してPRを自動生成
   - テンプレートから基本ファイルを自動生成

2. **必要なリソースの定義**
   - S3バケット（暗号化、バージョニング、ライフサイクル設定込み）
   - 専用IAMユーザーとアクセスキー
   - SSMパラメータ（認証情報の安全な格納）
   - TFAction設定（CI/CD自動化）

3. **既存インフラとの統合**
   - External Secrets Operatorは既にワイルドカード権限を持つため追加設定不要
   - `tfaction-root.yaml`で定義されたAWS IAMロールとCI/CD設定を活用
   - 標準的なarch プロジェクトの構造とパターンに準拠

## 参考資料

- [Longhorn Backup Documentation](https://longhorn.io/docs/latest/snapshots-and-backups/backup-and-restore/set-backup-target/)
- [AWS S3 Documentation](https://docs.aws.amazon.com/s3/)
- [boxp/arch Repository](https://github.com/boxp/arch)