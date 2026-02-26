# n8n on lolice cluster 設計ドキュメント（External Secrets前提）

## 1. 概要

### 1.1 目的
lolice cluster上でワークフロー自動化プラットフォーム [n8n](https://n8n.io) を安全に運用するための設計を定義する。シークレット管理にはExternal Secrets Operator（ESO）を使用し、既存のClusterSecretStore（AWS SSM Parameter Store）と統合する。

### 1.2 期待効果
- ワークフロー自動化基盤の確立（Webhook、定期実行、API連携等）
- 既存lolice clusterのGitOpsパターンとの一貫性維持
- シークレットの安全な管理（平文コミット禁止）
- Longhornによるデータ永続化とバックアップ対応

### 1.3 依存関係
| 依存先 | 用途 | 状態 |
|--------|------|------|
| ArgoCD | GitOps同期 | 既存稼働中 |
| External Secrets Operator (v0.18.2) | AWS SSM → K8s Secret同期 | 既存稼働中 |
| ClusterSecretStore `parameterstore` | AWS SSM Parameter Store接続 | 既存稼働中 |
| Longhorn | 永続ストレージ (PVC) | 既存稼働中 |
| Cloudflare Tunnel | 外部アクセス | 既存稼働中 |
| arch (Terraform) | AWS SSMパラメータ、IAM | 既存稼働中 |

---

## 2. アーキテクチャ

### 2.1 構成図

```
┌─────────────────────────────────────────────────────────────────┐
│  External                                                       │
│  ┌──────────────┐     ┌──────────────────┐                      │
│  │ Webhook/User │────▶│ Cloudflare Edge   │                      │
│  └──────────────┘     └────────┬─────────┘                      │
│                                │                                 │
├────────────────────────────────┼─────────────────────────────────┤
│  lolice cluster (namespace: n8n)                                │
│                                │                                 │
│  ┌─────────────────┐          ▼                                 │
│  │  cloudflared     │◀── Cloudflare Tunnel ──▶ n8n:5678         │
│  │  (Deployment)    │                                           │
│  └────────┬────────┘                                            │
│           │ TCP:5678                                             │
│           ▼                                                      │
│  ┌─────────────────┐     ┌─────────────────┐                    │
│  │  n8n             │────▶│  PostgreSQL      │                    │
│  │  (Deployment)    │     │  (StatefulSet)   │                    │
│  │  port: 5678      │     │  port: 5432      │                    │
│  └────────┬────────┘     └────────┬────────┘                    │
│           │                       │                              │
│  ┌────────▼────────┐    ┌────────▼────────┐                     │
│  │  n8n-data PVC   │    │  postgres-data   │                     │
│  │  (Longhorn 5Gi) │    │  PVC             │                     │
│  └─────────────────┘    │  (Longhorn 10Gi) │                     │
│                          └─────────────────┘                     │
│                                                                  │
│  ┌─────────────────────────────────────────────┐                │
│  │  ExternalSecret → K8s Secret                │                │
│  │  (AWS SSM Parameter Store経由)              │                │
│  │  - n8n-credentials (ENCRYPTION_KEY等)       │                │
│  │  - postgres-credentials (DB_PASSWORD等)     │                │
│  └─────────────────────────────────────────────┘                │
│                                                                  │
│  ┌─────────────────────────────────────────────┐                │
│  │  NetworkPolicy                              │                │
│  │  - n8n: cloudflared→5678, n8n→postgres:5432│                │
│  │  - postgres: n8n→5432のみ                   │                │
│  │  - cloudflared: Cloudflare Edge出力のみ     │                │
│  └─────────────────────────────────────────────┘                │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────┐
│  AWS (arch project管理)                  │
│  ┌──────────────────────────────────┐    │
│  │  SSM Parameter Store             │    │
│  │  /lolice/n8n/ENCRYPTION_KEY     │    │
│  │  /lolice/n8n/DB_POSTGRESDB_PASSWORD │ │
│  │  /lolice/n8n/TUNNEL_TOKEN       │    │
│  └──────────────────────────────────┘    │
└──────────────────────────────────────────┘
```

### 2.2 External Secrets データフロー

```
AWS SSM Parameter Store
  │
  │ (ESO が1時間ごとにポーリング)
  ▼
ClusterSecretStore "parameterstore"
  │ (IAM Role: external_secrets_role)
  ▼
ExternalSecret (namespace: n8n)
  │
  ├── n8n-credentials ExternalSecret
  │     ├── /lolice/n8n/ENCRYPTION_KEY    → ENCRYPTION_KEY
  │     └── /lolice/n8n/N8N_BASIC_AUTH_PASSWORD → (optional)
  │
  ├── postgres-credentials ExternalSecret
  │     └── /lolice/n8n/DB_POSTGRESDB_PASSWORD → POSTGRES_PASSWORD
  │
  └── tunnel-credentials ExternalSecret
        └── /lolice/n8n/TUNNEL_TOKEN      → tunnel-token
  │
  ▼
Kubernetes Secret (自動生成・自動更新)
  │
  ├── n8n Deployment (envFrom / env.valueFrom.secretKeyRef)
  ├── PostgreSQL StatefulSet (env.valueFrom.secretKeyRef)
  └── cloudflared Deployment (env.valueFrom.secretKeyRef)
```

---

## 3. Kubernetesリソース一覧

| リソース種別 | 名前 | 役割 |
|-------------|------|------|
| Namespace | `n8n` | アプリケーション分離 |
| Deployment | `n8n` | n8n本体 (1 replica, Recreate) |
| StatefulSet | `postgres` | PostgreSQLデータベース |
| Deployment | `cloudflared` | Cloudflare Tunnelプロキシ |
| Service (ClusterIP) | `n8n` | n8n内部通信 (port 5678) |
| Service (ClusterIP) | `postgres` | DB内部通信 (port 5432) |
| PVC | `n8n-data` | n8nファイル保存 (5Gi, Longhorn) |
| PVC | `postgres-data` | PostgreSQLデータ (10Gi, Longhorn) |
| ExternalSecret | `n8n-credentials` | n8n暗号化キー等 |
| ExternalSecret | `postgres-credentials` | DBパスワード |
| ExternalSecret | `tunnel-credentials` | Cloudflareトンネルトークン |
| NetworkPolicy | `n8n-network-policy` | n8n Pod通信制御 |
| NetworkPolicy | `postgres-network-policy` | DB Pod通信制御 |
| NetworkPolicy | `cloudflared-network-policy` | Tunnel Pod通信制御 |
| Application (ArgoCD) | `n8n` | GitOps同期定義 |

---

## 4. セキュリティ要件

### 4.1 シークレット管理
- すべてのシークレットはAWS SSM Parameter Store → ESOで供給
- Kubernetes Secretの直書き・平文コミット禁止
- `deletionPolicy: Retain` で誤削除防止
- `refreshInterval: 1h` で自動ローテーション反映

### 4.2 認証・アクセス制御
- Cloudflare Access（Zero Trust）で認証前段を構築
- n8n自体のBasic Auth or ユーザー管理を有効化
- Cloudflare Tunnel経由のみ外部アクセス可（LoadBalancer/NodePort不使用）

### 4.3 Pod セキュリティ
- `runAsNonRoot: true`
- `readOnlyRootFilesystem: false`（n8nが書き込み必要）
- `allowPrivilegeEscalation: false`
- `automountServiceAccountToken: false`

### 4.4 ネットワーク制限
- n8n: cloudflaredからの5678ポートのみ受信、postgres:5432への出力、外部HTTPS出力（Webhook送信用）
- PostgreSQL: n8nからの5432ポートのみ受信、出力なし
- cloudflared: Cloudflare Edge（HTTPS/QUIC）への出力のみ

### 4.5 最小権限
- ServiceAccount自動マウント無効
- RBAC権限なし（アプリレベルで不要）
- ストレージアクセスはPVCスコープ内に限定

---

## 5. 運用手順

### 5.1 バックアップ

#### PostgreSQL
```bash
# Pod内でダンプ
kubectl exec -n n8n statefulset/postgres -- \
  pg_dump -U n8n -d n8n --clean --if-exists > n8n_backup_$(date +%Y%m%d).sql

# ローカルにコピー
kubectl cp n8n/postgres-0:/tmp/backup.sql ./n8n_backup.sql
```

#### Longhorn Snapshot
- Longhorn UIまたはAPIからPVCスナップショットを作成
- `longhorn-backup-secret`設定済みの場合はS3バックアップも可能

#### 推奨頻度
- PostgreSQL論理バックアップ: 日次
- Longhornスナップショット: 日次（自動設定推奨）
- S3バックアップ: 週次

### 5.2 リストア

#### PostgreSQLリストア
```bash
# バックアップファイルをPodにコピー
kubectl cp ./n8n_backup.sql n8n/postgres-0:/tmp/backup.sql

# リストア実行
kubectl exec -n n8n statefulset/postgres -- \
  psql -U n8n -d n8n -f /tmp/backup.sql
```

#### PVCリストア（Longhorn）
1. Longhorn UIでスナップショット or バックアップを選択
2. PVCをリストア先に指定
3. n8n Deploymentを再起動

### 5.3 鍵ローテーション

#### 手順
1. AWS SSM Parameter Storeで新しい値を設定（archプロジェクト経由 or AWS CLI）
2. ESOの`refreshInterval: 1h`で自動反映、または手動トリガー:
   ```bash
   kubectl annotate externalsecret -n n8n n8n-credentials \
     force-sync=$(date +%s) --overwrite
   ```
3. Reloaderが検知してn8n Deploymentを自動再起動
4. n8nログインとワークフロー実行を確認

#### 注意事項
- `ENCRYPTION_KEY` はn8nのクレデンシャル暗号化に使用される。変更するとn8nに保存されたクレデンシャルが復号不能になる。初期設定後は変更しないこと
- `DB_POSTGRESDB_PASSWORD` の変更時はPostgreSQL側のパスワードも同時に更新が必要

### 5.4 障害時対応

| 障害 | 対応 |
|------|------|
| n8n Pod CrashLoop | `kubectl logs -n n8n deploy/n8n` でログ確認、環境変数・DB接続を検証 |
| PostgreSQL起動失敗 | PVC状態確認、`kubectl describe pvc -n n8n`、Longhornボリューム確認 |
| ExternalSecret同期失敗 | `kubectl get externalsecret -n n8n` でステータス確認、AWS IAM権限検証 |
| Tunnel接続不可 | cloudflared Pod ログ確認、トンネルトークン有効性確認 |
| ワークフロー実行失敗 | n8n UIでエラーログ確認、外部API疎通確認 |

---

## 6. 導入ステップ

### Phase 1: PoC（最小検証）
1. AWS SSM Parameter Storeに必要パラメータを登録（archプロジェクト経由推奨）
   - `/lolice/n8n/ENCRYPTION_KEY`
   - `/lolice/n8n/DB_POSTGRESDB_PASSWORD`
   - `/lolice/n8n/TUNNEL_TOKEN`
2. `argoproj/n8n/` ディレクトリにKubernetesマニフェストを配置
3. `argoproj/kustomization.yaml` にn8n Applicationを追加
4. PRを作成し、ArgoCD diffで変更を確認
5. マージ後、ArgoCDが自動同期
6. n8n UIにアクセスし、基本的なワークフロー（HTTPリクエスト等）を実行確認

### Phase 2: セキュリティ強化
1. NetworkPolicyの適用と検証
2. Cloudflare Access（Zero Trust）で認証前段を設定
3. n8nユーザー管理設定（Basic Auth or ユーザーDB）
4. Pod Security Contextの最終調整

### Phase 3: 本番運用
1. Longhornバックアップスケジュール設定
2. PostgreSQL定期バックアップCronJob設定
3. Prometheus/Grafanaでn8nメトリクス監視設定
4. アラート設定（Pod再起動、DB接続エラー等）
5. 運用ランブック整備

---

## 7. 設計レビュー観点チェックリスト

### アーキテクチャ
- [ ] n8n、PostgreSQL、cloudflaredが各々独立したPodで動作する
- [ ] Pod間通信がServiceを介して行われる
- [ ] 外部アクセスがCloudflare Tunnel経由のみである

### シークレット管理
- [ ] すべてのシークレットがExternalSecret経由で供給される
- [ ] 平文のシークレットがリポジトリにコミットされていない
- [ ] ClusterSecretStore `parameterstore` を参照している
- [ ] `deletionPolicy: Retain` が設定されている

### ストレージ
- [ ] PVCが `longhorn` StorageClassを使用している
- [ ] PostgreSQLデータとn8nデータが別PVCに分離されている
- [ ] アクセスモードが `ReadWriteOnce` である

### ネットワーク
- [ ] NetworkPolicyが全Pod種別に定義されている
- [ ] PostgreSQLへのアクセスがn8n Podに限定されている
- [ ] 不要な外部通信が制限されている

### 運用
- [ ] バックアップ手順が文書化されている
- [ ] リストア手順が文書化されている
- [ ] 鍵ローテーション手順が文書化されている
- [ ] 障害対応表が用意されている

### 既存lolice clusterとの整合性
- [ ] ArgoCD Application形式に準拠している
- [ ] Kustomize構成（base/overlays or フラット）に従っている
- [ ] ExternalSecret APIバージョンが `external-secrets.io/v1` である
- [ ] Namespace命名規則に違反していない
- [ ] `argoproj/kustomization.yaml` にApplication参照が追加されている
- [ ] CloudflaredのDeploymentパターンが既存（boxp-home等）と一貫している

### テスト確認方法
- [ ] `kustomize build argoproj/n8n/` が成功する
- [ ] `kubectl apply --dry-run=client -f` でバリデーションが通る
- [ ] ArgoCD diff（GitHub Actions）で意図通りの差分が出る
- [ ] n8n UIにログインできる（デプロイ後）
- [ ] サンプルワークフロー（HTTP Request等）が正常実行される（デプロイ後）

---

## 8. リスクと未確定事項

### リスク
| リスク | 影響 | 緩和策 |
|--------|------|--------|
| PostgreSQL単一Pod構成 | DB障害時にn8nが停止 | Longhornバックアップ + リストア手順整備 |
| n8n暗号化キー喪失 | 保存済みクレデンシャル復号不能 | SSMパラメータの厳格な管理、変更禁止運用 |
| Longhornボリューム障害 | データ喪失 | レプリカ数2（Longhorn標準）+ S3バックアップ |

### 未確定事項
| 項目 | 選択肢 | 決定時期 |
|------|--------|----------|
| n8n認証方式 | Basic Auth / ユーザーDB / Cloudflare Access Only | Phase 2 |
| Cloudflare Tunnelの公開ドメイン | サブドメイン選定 | Phase 1前 |
| PostgreSQLバックアップ自動化 | CronJob / 外部スクリプト | Phase 3 |
| n8nバージョン固定方針 | Renovate自動更新 / 手動管理 | Phase 1後 |
| メトリクス収集方式 | Prometheus Exporter / n8n組み込み | Phase 3 |

---

## 9. 付録: AWS SSM パラメータ一覧（archプロジェクトで作成が必要）

| SSMパラメータパス | 用途 | タイプ |
|------------------|------|--------|
| `/lolice/n8n/ENCRYPTION_KEY` | n8nクレデンシャル暗号化キー | SecureString |
| `/lolice/n8n/DB_POSTGRESDB_PASSWORD` | PostgreSQL接続パスワード | SecureString |
| `/lolice/n8n/TUNNEL_TOKEN` | Cloudflare Tunnelトークン | SecureString |
