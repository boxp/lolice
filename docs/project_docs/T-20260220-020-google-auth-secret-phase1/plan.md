# T-20260220-020: Google連携 Phase 1 — 認証・Secret基盤整備

## 概要

Google Calendar API / Google Maps API をOpenClawから利用するために必要な
OAuth2 / API Key の保管・注入・更新/失効手順を整備する。

**前提計画書**: `arch` リポジトリ `docs/project_docs/openclaw/T-20260220-005-google-calendar-maps-plan.md`

## スコープ

| # | 項目 | 対象リポジトリ |
|---|------|---------------|
| 1 | SSM Parameter Store にクレデンシャルを定義 | arch |
| 2 | ExternalSecret で K8s Secret へ同期 | lolice |
| 3 | initContainer + emptyDir による OAuth token ファイル注入 (runAsUser=1000) | lolice |
| 4 | env var による API key / client credentials 注入 | lolice |
| 5 | ローテーション・失効・緊急停止手順のドキュメント化 | lolice |
| 6 | 監査ログ方針の策定 | lolice |

## 非スコープ

- babashka CLI本体実装 (Phase 2)
- OpenClawスキル接続 (Phase 3)
- Google Cloud Project 作成・OAuth同意画面設定 (運用者手動作業)

## アーキテクチャ

```
┌─────────────────┐     Terraform      ┌──────────────────────┐
│ Google Cloud     │                    │ AWS SSM              │
│ Console          │                    │ Parameter Store      │
│ (手動設定)       │                    │ /lolice/openclaw/*   │
└────────┬────────┘                    └──────────┬───────────┘
         │                                        │
         │ Client ID / Secret / Refresh Token     │ ExternalSecret (1h refresh)
         │ Maps API Key                           │
         └────────► SSM に手動投入 ──────────────►│
                                                   ▼
                                          ┌────────────────────┐
                                          │ K8s Secret          │
                                          │ google-oauth-       │
                                          │ credentials         │
                                          └────────┬───────────┘
                                                   │
                              ┌─────────────────────┴─────────────────────┐
                              │                                           │
                    ┌─────────▼────────┐                      ┌──────────▼──────────┐
                    │ initContainer     │                      │ openclaw container   │
                    │ init-google-oauth  │                      │ env vars:            │
                    │ → oauth.json      │                      │ GOOGLE_OAUTH_CLIENT_*│
                    │   (emptyDir)      │                      │ GOOGLE_MAPS_API_KEY  │
                    └─────────┬────────┘                      │ GOOGLE_OAUTH_TOKEN_  │
                              │                                │   PATH               │
                              ▼                                └──────────────────────┘
                    ┌──────────────────┐
                    │ emptyDir          │
                    │ /home/node/       │
                    │   .google/        │
                    │   oauth.json      │
                    │ (0600, 1000:1000) │
                    └──────────────────┘
```

## SSM パラメータ一覧

| パラメータパス | 用途 | 初期値 |
|---------------|------|--------|
| `/lolice/openclaw/GOOGLE_OAUTH_CLIENT_ID` | Calendar API OAuth2 Client ID | dummy (手動更新) |
| `/lolice/openclaw/GOOGLE_OAUTH_CLIENT_SECRET` | Calendar API OAuth2 Client Secret | dummy (手動更新) |
| `/lolice/openclaw/GOOGLE_OAUTH_REFRESH_TOKEN` | Calendar API OAuth2 Refresh Token | dummy (手動更新) |
| `/lolice/openclaw/GOOGLE_MAPS_API_KEY` | Maps Geocoding/Directions API Key | dummy (手動更新) |

## ExternalSecret 設計

| リソース名 | 生成Secret名 | 用途 |
|-----------|-------------|------|
| `google-oauth-es` | `google-oauth-credentials` | Google OAuth / Maps credentials |

既存の `openclaw-credentials` とは別Secret。理由:
- セキュリティ分離: Google認証情報は独立した責務
- 独立ローテーション: Google credentials のみ更新可能
- 監査明確化: Secret参照ログで用途が明確

## runAsUser=1000 token ファイル権限設計

| 項目 | 設計 |
|------|------|
| ファイルパス | `/home/node/.google/oauth.json` |
| ボリューム種別 | `emptyDir` (sizeLimit: 1Mi) |
| ファイルパーミッション | `0600` |
| オーナー | `1000:1000` (fsGroup=1000 + runAsUser=1000) |
| 書き込み | bb スクリプトが access_token リフレッシュ時に書き戻し |
| Pod再起動時 | emptyDir 消失 → initContainer が Secret から再生成 |

## 環境変数注入

| 変数名 | ソース | 注入先コンテナ |
|--------|--------|---------------|
| `GOOGLE_OAUTH_CLIENT_ID` | google-oauth-credentials | openclaw |
| `GOOGLE_OAUTH_CLIENT_SECRET` | google-oauth-credentials | openclaw |
| `GOOGLE_MAPS_API_KEY` | google-oauth-credentials | openclaw |
| `GOOGLE_OAUTH_TOKEN_PATH` | 固定値 `/home/node/.google/oauth.json` | openclaw |

## リスク

| リスク | 影響度 | 緩和策 |
|--------|--------|--------|
| SSM dummy値のまま運用開始 | 低 | bb スクリプト (Phase 2) が HTTP 401 で明示的にエラー |
| emptyDir の Pod 再起動時消失 | 低 | initContainer が毎回 Secret から再生成 |
| refresh_token の Google 側ローテーション | 中 | ローテーション手順を docs に明記、モニタリングで検知 |

## 次アクション

1. Google Cloud Console で OAuth 同意画面設定・クレデンシャル作成 (運用者)
2. AWS SSM Parameter Store に実際の値を手動投入 (運用者)
3. Phase 2: babashka スクリプト実装
4. Phase 3: OpenClaw スキル接続
