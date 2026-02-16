# OpenClaw Telegram Bot Token セットアップ

## 概要

OpenClawのTelegram接続用トークンは、AWS SSM Parameter Store経由でExternalSecretとして管理されています。
実トークン値はGitリポジトリにコミットされません。

## Secret登録手順

1. Telegram BotFatherからBot Tokenを取得する
2. AWS SSM Parameter Storeにトークンを登録する:

```bash
aws ssm put-parameter \
  --name "/lolice/openclaw/TELEGRAM_BOT_TOKEN" \
  --type "SecureString" \
  --value "<YOUR_TELEGRAM_BOT_TOKEN>" \
  --region ap-northeast-1
```

3. External Secrets Operatorが自動的にSSMから値を取得し、`openclaw-credentials` Secretに `TELEGRAM_BOT_TOKEN` キーとして同期します（更新間隔: 1時間）

## 参照フロー

```
AWS SSM Parameter Store
  /lolice/openclaw/TELEGRAM_BOT_TOKEN
    ↓ (External Secrets Operator, 1h interval)
K8s Secret: openclaw-credentials
  key: TELEGRAM_BOT_TOKEN
    ↓ (secretKeyRef)
Env var: TELEGRAM_BOT_TOKEN
    ↓ (${TELEGRAM_BOT_TOKEN} in openclaw.json)
OpenClaw config: channels.telegram.botToken
```

## 即時反映

SSMの値を更新した後、即時反映が必要な場合:

```bash
# ExternalSecretを手動で再同期
kubectl annotate externalsecret openclaw-es -n openclaw \
  force-sync=$(date +%s) --overwrite
```
