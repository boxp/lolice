# Google Credentials 運用手順書

OpenClaw が利用する Google Calendar API (OAuth2) / Google Maps API (API Key) の
クレデンシャル運用手順。

## 1. アーキテクチャ概要

```
Google Cloud Console
  └─ OAuth2 Client (Calendar)  ──► SSM /lolice/openclaw/GOOGLE_OAUTH_*
  └─ API Key (Maps)            ──► SSM /lolice/openclaw/GOOGLE_MAPS_API_KEY
                                        │
                                        ▼
                                   ExternalSecret (1h refresh)
                                        │
                                        ▼
                                   K8s Secret: google-oauth-credentials
                                        │
                           ┌────────────┴────────────┐
                           ▼                          ▼
                   initContainer              env vars
                   → oauth.json               → GOOGLE_OAUTH_CLIENT_ID
                     (emptyDir, 0600)         → GOOGLE_OAUTH_CLIENT_SECRET
                                              → GOOGLE_MAPS_API_KEY
                                              → GOOGLE_OAUTH_TOKEN_PATH
```

### 責務分離

| レイヤー | 責務 | リポジトリ |
|----------|------|-----------|
| AWS SSM Parameter Store | クレデンシャル原本保管 (SecureString) | arch (Terraform) |
| External Secrets Operator | SSM → K8s Secret 同期 (1h間隔) | lolice |
| initContainer `init-google-oauth` | Secret → emptyDir にtoken file生成 | lolice |
| env vars | コンテナへの credential 注入 | lolice |
| babashka スクリプト | access_token 自動リフレッシュ | arch (openclaw image) |

## 2. ローテーション手順

### 2.1 OAuth Client Secret ローテーション

**トリガー**: 定期ローテーション (推奨: 年1回) またはセキュリティインシデント

```bash
# 1. Google Cloud Console で新しい Client Secret を生成
#    API & Services > Credentials > OAuth 2.0 Client IDs > Edit

# 2. AWS SSM Parameter Store を更新
aws ssm put-parameter \
  --name "/lolice/openclaw/GOOGLE_OAUTH_CLIENT_SECRET" \
  --value "<NEW_CLIENT_SECRET>" \
  --type SecureString \
  --overwrite \
  --region ap-northeast-1

# 3. ExternalSecret が同期するのを待つ (最大1時間)
#    または強制同期:
kubectl annotate externalsecret google-oauth-es \
  -n openclaw \
  force-sync=$(date +%s) --overwrite

# 4. Pod を再起動して新しい Secret を読み込み
kubectl rollout restart deployment/openclaw -n openclaw

# 5. 動作確認
kubectl logs -n openclaw deployment/openclaw -c openclaw | tail -20
```

### 2.2 OAuth Refresh Token ローテーション

**トリガー**: トークン失効、Google側の強制ローテーション、セキュリティインシデント

```bash
# 1. ローカル環境で OAuth 再認証 (ブラウザ必要)
#    bb スクリプトの認証フロー (Phase 2 で実装) を実行
#    → 新しい refresh_token を取得

# 2. AWS SSM Parameter Store を更新
aws ssm put-parameter \
  --name "/lolice/openclaw/GOOGLE_OAUTH_REFRESH_TOKEN" \
  --value "<NEW_REFRESH_TOKEN>" \
  --type SecureString \
  --overwrite \
  --region ap-northeast-1

# 3. ExternalSecret 強制同期
kubectl annotate externalsecret google-oauth-es \
  -n openclaw \
  force-sync=$(date +%s) --overwrite

# 4. Pod 再起動
kubectl rollout restart deployment/openclaw -n openclaw
```

### 2.3 Google Maps API Key ローテーション

**トリガー**: 定期ローテーション (推奨: 年1回) またはセキュリティインシデント

```bash
# 1. Google Cloud Console で新しい API Key を発行
#    API & Services > Credentials > Create API Key
#    制限設定:
#    - Application restrictions: IP addresses (サーバーIP)
#    - API restrictions: Geocoding API, Directions API のみ

# 2. AWS SSM Parameter Store を更新
aws ssm put-parameter \
  --name "/lolice/openclaw/GOOGLE_MAPS_API_KEY" \
  --value "<NEW_API_KEY>" \
  --type SecureString \
  --overwrite \
  --region ap-northeast-1

# 3. ExternalSecret 強制同期 → Pod 再起動
kubectl annotate externalsecret google-oauth-es \
  -n openclaw \
  force-sync=$(date +%s) --overwrite
kubectl rollout restart deployment/openclaw -n openclaw

# 4. 旧 API Key を Google Cloud Console で無効化
```

## 3. 失効・緊急停止手順

### 3.1 緊急停止 (全 Google API アクセスを即座に遮断)

**所要目標時間**: 15分以内

```bash
# 方法 A: Pod 停止 (最速)
kubectl scale deployment/openclaw -n openclaw --replicas=0

# 方法 B: Google Cloud Console でクレデンシャル無効化
# OAuth: API & Services > Credentials > OAuth Client > Delete/Disable
# API Key: API & Services > Credentials > API Key > Restrict/Delete

# 方法 C: SSM パラメータを無効値に上書き
aws ssm put-parameter \
  --name "/lolice/openclaw/GOOGLE_OAUTH_REFRESH_TOKEN" \
  --value "REVOKED" \
  --type SecureString \
  --overwrite \
  --region ap-northeast-1

aws ssm put-parameter \
  --name "/lolice/openclaw/GOOGLE_MAPS_API_KEY" \
  --value "REVOKED" \
  --type SecureString \
  --overwrite \
  --region ap-northeast-1

# 強制同期 → Pod 再起動
kubectl annotate externalsecret google-oauth-es \
  -n openclaw \
  force-sync=$(date +%s) --overwrite
kubectl rollout restart deployment/openclaw -n openclaw
```

### 3.2 OAuth トークン失効

```bash
# Google Cloud Console から失効
# Security > Third-party apps with account access > Remove access

# または Google OAuth revoke endpoint
curl -d "token=<ACCESS_OR_REFRESH_TOKEN>" \
  https://oauth2.googleapis.com/revoke
```

### 3.3 API Key 漏えい対応

| ステップ | 操作 | 担当 |
|----------|------|------|
| 1 | Google Cloud Console で該当 API Key を即座に無効化 | 運用者 |
| 2 | 新しい API Key を発行 (IP制限・API制限付き) | 運用者 |
| 3 | SSM パラメータ更新 → ExternalSecret 同期 → Pod 再起動 | 運用者 |
| 4 | Cloud Console の使用状況レポートで不正利用の有無を確認 | 運用者 |
| 5 | インシデントレポート作成 | 運用者 |

### 3.4 インシデント時ローテーション SLA

| イベント | 目標時間 | 検知方法 |
|----------|----------|----------|
| API Key 漏えい | 15分以内に無効化 | GitHub Secret Scanning / Grafana OnCall |
| OAuth トークン漏えい | 15分以内に無効化 | Cloud Console 監査ログ / Grafana OnCall |
| Client Secret 漏えい | 30分以内に再生成 | GitHub Secret Scanning / 手動報告 |

## 4. 監査ログ方針

### 4.1 参照イベント

| イベント | ログソース | 保持期間 |
|----------|-----------|----------|
| SSM Parameter Store GetParameter | AWS CloudTrail | 90日 (デフォルト) |
| ExternalSecret 同期 | External Secrets Operator Pod ログ | Pod ライフタイム |
| Pod 起動時の Secret マウント | kubelet ログ | ノードログ保持期間 |
| bb スクリプトの API 呼び出し | OpenClaw Pod ログ (stdout) | Pod ライフタイム |

### 4.2 更新イベント

| イベント | ログソース | アラート |
|----------|-----------|---------|
| SSM パラメータ更新 (PutParameter) | AWS CloudTrail | CloudWatch Alarm → Grafana OnCall |
| ExternalSecret 同期失敗 | External Secrets Operator メトリクス | Prometheus Alert → Grafana OnCall |
| OAuth token リフレッシュ失敗 (HTTP 400/401) | OpenClaw Pod ログ | ログベースアラート (Phase 4) |
| API Key 使用量急増 | Google Cloud Console | Budget Alert |

### 4.3 CloudTrail フィルタ例

```json
{
  "eventSource": "ssm.amazonaws.com",
  "eventName": ["PutParameter", "GetParameter", "DeleteParameter"],
  "requestParameters": {
    "name": "/lolice/openclaw/GOOGLE_*"
  }
}
```

### 4.4 ExternalSecret 同期監視

```promql
# ExternalSecret 同期ステータス監視 (Prometheus)
externalsecret_status_condition{
  name="google-oauth-es",
  namespace="openclaw",
  condition="Ready"
} == 0
```

## 5. 前提条件チェックリスト

Phase 1 (本PRでの実装) 完了後、実際にクレデンシャルを投入する前に確認すべき事項:

- [ ] Google Cloud Project 作成済み
- [ ] OAuth 同意画面設定済み (テストモード)
- [ ] Calendar API 有効化済み
- [ ] Geocoding API / Directions API 有効化済み
- [ ] OAuth Client ID / Secret 発行済み
- [ ] Maps API Key 発行済み (IP制限付き)
- [ ] ローカルで OAuth 認証完了、refresh_token 取得済み
- [ ] SSM に実際のクレデンシャル値を投入済み
- [ ] ExternalSecret の同期を確認済み
- [ ] Pod 再起動後、oauth.json が正しく生成されることを確認済み
