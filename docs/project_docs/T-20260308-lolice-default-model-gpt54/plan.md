# T-20260308-lolice-default-model-gpt54

## 目的
OpenClawのデフォルトモデルを `openai-codex/gpt-5.4` (alias: `gpt54`) に変更する。

## 変更内容

### 対象ファイル
- `argoproj/openclaw/configmap-openclaw.yaml`

### 変更箇所
- `agents.defaults.model.primary`: `openai-codex/gpt-5.3-codex` → `openai-codex/gpt-5.4`

### 整合性確認
- `openai-codex/gpt-5.4` は既に `agents.defaults.models` セクションに alias `gpt54` として登録済み
- fallbacks設定は変更不要
- heartbeat用モデル (`openai-codex/gpt-5.1-codex-mini`) はデフォルトモデルとは独立のため変更不要

## 非スコープ
- heartbeatモデルの変更
- litellmプロバイダー設定の変更
- fallbacksリストの変更
