# OpenClaw primary/fallback モデルを openai-codex 系へ統一

## 概要
OpenClawのデフォルトモデル設定を openai-codex プロバイダに統一する。

## 変更内容

### 対象ファイル
- `argoproj/openclaw/configmap-openclaw.yaml`

### 変更箇所

#### 1. Primary model の変更
- Before: `litellm/google/gemini-2.5-flash`
- After: `openai-codex/gpt-5.3-codex`

#### 2. Fallback models の統一
- Before: `litellm/anthropic/claude-sonnet-4-5-20250929`, `openai-codex/gpt-4o-mini`, `openai-codex/o4-mini`, `litellm/zai/glm-4.7`, `litellm/zai/glm-4.5-flash`
- After: `openai-codex/gpt-4o-mini`, `openai-codex/o4-mini`
- 非openai-codex fallback（anthropic, zai, google）を除外

#### 3. Alias 設定
- 変更不要。`openai-codex/gpt-5.3-codex` → `codex`, `openai-codex/gpt-4o-mini` → `gpt4o-mini`, `openai-codex/o4-mini` → `o4-mini` は既に定義済み

## 制約事項
- `models.providers` に `openai-codex` を直接定義しない（OAuth provider のため、PR #433 準拠）
- litellm プロバイダの models リストは変更しない（他のalias参照や手動切替用に残す）
