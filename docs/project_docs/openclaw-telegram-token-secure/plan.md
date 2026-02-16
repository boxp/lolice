# OpenClaw Telegram Token Secure 管理

## 概要

OpenClawのTelegram接続用トークンを、ExternalSecret経由でAWS SSM Parameter Storeから取得し、
環境変数として安全に注入できるようにする。

## 現状分析

- `argoproj/openclaw/configmap-openclaw.yaml` の `openclaw.json` でチャンネル設定を管理
- Discord Bot Tokenは `${DISCORD_BOT_TOKEN}` として環境変数参照で解決済み
- Telegram関連の設定は現在未定義
- `external-secret.yaml` → `openclaw-credentials` Secret を生成（SSMから同期）
- `deployment-openclaw.yaml` で `secretKeyRef` を使い個別環境変数を注入

## 変更計画

### 1. ExternalSecret に TELEGRAM_BOT_TOKEN を追加

**ファイル:** `argoproj/openclaw/external-secret.yaml`

`openclaw-credentials` Secret に `TELEGRAM_BOT_TOKEN` キーを追加。
SSMパス: `/lolice/openclaw/TELEGRAM_BOT_TOKEN`（既存の命名規則に準拠）

### 2. Deployment に環境変数を追加

**ファイル:** `argoproj/openclaw/deployment-openclaw.yaml`

`openclaw` コンテナの `env` セクションに以下を追加:
```yaml
- name: TELEGRAM_BOT_TOKEN
  valueFrom:
    secretKeyRef:
      name: openclaw-credentials
      key: TELEGRAM_BOT_TOKEN
```

### 3. ConfigMap に Telegram チャンネル設定を追加

**ファイル:** `argoproj/openclaw/configmap-openclaw.yaml`

`channels` セクションに Telegram 設定を追加:
```json
"telegram": {
  "enabled": true,
  "botToken": "${TELEGRAM_BOT_TOKEN}"
}
```

Discord と同じ `${VAR_NAME}` パターンでランタイム解決。

### 4. 運用メモの追加

**ファイル:** `doc/openclaw-telegram-setup.md`

AWS SSM Parameter Store へのトークン登録手順を記載。

## Secret設計サマリー

| 項目 | 値 |
|---|---|
| SSM Parameter Key | `/lolice/openclaw/TELEGRAM_BOT_TOKEN` |
| ExternalSecret名 | `openclaw-es` (既存) |
| 生成Secret名 | `openclaw-credentials` (既存) |
| Secret内キー | `TELEGRAM_BOT_TOKEN` |
| 環境変数名 | `TELEGRAM_BOT_TOKEN` |
| ConfigMap参照 | `${TELEGRAM_BOT_TOKEN}` |

## 差分最小の原則

- 既存ファイル3つを編集するのみ（新規リソースの追加なし）
- 既存のExternalSecret / Secret / 環境変数パターンに完全準拠
- ConfigMapのJSON構造を壊さない
