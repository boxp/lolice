# Fix: openai-codex プロバイダー設定修正

## 問題
PR #432 マージ後、`openclaw models auth login --provider openai-codex` コマンドで以下のバリデーションエラーが発生:
```
- models.providers.openai-codex.baseUrl: Invalid input: expected string, received undefined
- models.providers.openai-codex.api: Invalid input
```

## 根本原因
`openclaw/openclaw` の `ModelProviderSchema` (zod-schema.core.ts) において:
- `baseUrl` は `z.string().min(1)` で**必須フィールド**だが、`openai-codex` セクションには未定義
- `api` フィールドの有効値 (`ModelApiSchema`) に `"openai-codex"` は含まれていない
  - 有効値: `openai-completions`, `openai-responses`, `anthropic-messages`, `google-generative-ai`, `github-copilot`, `bedrock-converse-stream`, `ollama`
- `openai-codex` はOAuth認証で管理される特別なプロバイダーで、`models.providers` に定義するタイプではない

## 修正内容
`configmap-openclaw.yaml` の `models.providers` から `openai-codex` セクションを完全削除。

`agents.defaults.model` のモデル参照（`openai-codex/gpt-4o-mini`, `openai-codex/o4-mini`, `openai-codex/gpt-5.3-codex`）およびエイリアス設定はそのまま維持。これらはOAuth認証ログイン後に正常に動作する。

## 影響範囲
- `argoproj/openclaw/configmap-openclaw.yaml` のみ修正
- モデル参照・エイリアスは変更なし
