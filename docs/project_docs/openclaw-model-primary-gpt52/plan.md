# OpenClaw Model Update: primary=gpt-5.2, fallback追加 Gemini 2.5 Flash

## 概要
OpenClawのデフォルトモデル設定を更新する。

## 変更内容

### primary モデル変更
- Before: `openai-codex/gpt-5.3-codex`
- After: `openai-codex/gpt-5.2`

### fallback モデル変更
- Before: `["openai-codex/gpt-4o-mini", "openai-codex/o4-mini"]`
- After: `["litellm/google/gemini-2.5-flash", "openai-codex/gpt-4o-mini", "openai-codex/o4-mini"]`

### models エイリアス変更
- Before: `openai-codex/gpt-5.3-codex` (alias: `codex`)
- After: `openai-codex/gpt-5.2` (alias: `gpt52`)

## 変更ファイル
- `argoproj/openclaw/configmap-openclaw.yaml`

## 設計方針
- openai-codex providerはOAuth管理のため、providers定義は追加しない
- litellm/google/gemini-2.5-flash は既にlitellm configおよびmodelsセクションに定義済み
- 既存fallback（gpt-4o-mini, o4-mini）は維持し、Gemini 2.5 Flashを先頭に追加
