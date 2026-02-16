# OpenClaw GPTモデル統合計画（gpt-4o-mini サブスクリプション利用）

## 概要

OpenClawがサブスクリプションベースのGPTモデル `gpt-4o-mini` を利用できるように、lolice cluster上のKubernetesマニフェストを修正する。

## 現状分析

### 既に設定済みの項目
- **OPENAI_API_KEY**: ExternalSecret (`openclaw-es`, `litellm-es`) で AWS SSM (`/lolice/openclaw/OPENAI_API_KEY`) から取得済み
- **LiteLLM**: `openai/gpt-5-mini`, `openai/gpt-5.1-codex-mini` が既にmodel_listに存在
- **OpenClaw ConfigMap**: 上記2モデルがproviders/modelsに定義済み
- **Deployment**: `OPENAI_API_KEY` 環境変数が既にopenclawコンテナとlitellmコンテナに設定済み
- **NetworkPolicy**: LiteLLM Podは外部HTTPS (443) へのegress許可済み → api.openai.com へのアクセス可能

### 追加が必要な項目
- `gpt-4o-mini` モデル定義の追加（LiteLLM ConfigMap + OpenClaw ConfigMap）

## 変更内容

### 1. `configmap-litellm.yaml`
- `openai/gpt-4o-mini` モデルをmodel_listに追加
- `OPENAI_API_KEY` で認証（既存のキーを共有）

### 2. `configmap-openclaw.yaml`
- `models.providers.litellm.models` に `openai/gpt-4o-mini` のモデル定義を追加
  - contextWindow: 128000, maxTokens: 16384, reasoning: false
  - input: ["text", "image"]（GPT-4o miniはマルチモーダル対応）
- `agents.defaults.model.fallbacks` にフォールバックとして追加
- `agents.defaults.models` にエイリアス `gpt4o-mini` を追加

### 3. 変更不要な項目
- **ExternalSecret**: OPENAI_API_KEY は既に設定済み
- **Deployment**: OPENAI_API_KEY 環境変数は既に設定済み
- **NetworkPolicy**: 外部 HTTPS アクセスは既に許可済み

## GPT-4o mini モデルスペック
- Context window: 128,000 tokens
- Max output tokens: 16,384 tokens
- Reasoning: なし（`false`）
- 入力: テキスト + 画像（マルチモーダル）
- エンドポイント: api.openai.com（標準OpenAI API）

## リスク・注意点
- 既存の `OPENAI_API_KEY` がサブスクリプションに紐づいていることが前提
- LiteLLMの `drop_params: true` 設定により、gpt-4o-miniが対応しないパラメータは自動的に無視される
- 月間予算 $50 / 30日の制限は全OpenAIモデルで共有
