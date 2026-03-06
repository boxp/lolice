# T-20260305-010: OpenClawモデル選択にGPT-5.4系を追加（Codex provider経由）

## 目的
boxp/lolice上のOpenClawでGPT-5.4系モデルをCodex provider経由で選択できるようにし、コスト事故を回避する。

## 変更対象ファイル

### 1. `argoproj/openclaw/configmap-litellm.yaml`
- GPT-5.4 / GPT-5.4 mini のLiteLLMルーティング定義を**除去**
- LiteLLM経由でのコスト事故を防止

### 2. `argoproj/openclaw/configmap-openclaw.yaml`
- `models.providers.litellm.models[]` からGPT-5.4系エントリを**除去**
- `agents.defaults.models` にCodex providerエイリアスを追加
  - `openai-codex/gpt-5.4` → alias: `gpt54`
  - `openai-codex/gpt-5.4-mini` → alias: `gpt54-mini`

## 設計判断

### プロバイダー選択: Codex provider（LiteLLM経由禁止）
- GPT-5.4系はCodex provider（`openai-codex/`プレフィックス）経由で提供
- 既存のCodex providerモデル（`gpt-5.3-codex`, `gpt-5.1-codex-mini`, `gpt-4o-mini`, `o4-mini`）と同じ方式
- LiteLLM経由にしないことで、LiteLLMのbudget制御外での意図しないコスト発生を回避
- OpenAI APIキーはCodex provider側で直接使用

### モデルバリエーション
- `gpt-5.4`: 標準モデル（マルチモーダル対応）
- `gpt-5.4-mini`: 軽量・高速バリアント（既存のmini命名規約に準拠）

### エイリアス命名
- 既存パターン: `gpt4o-mini`, `o4-mini`, `codex`, `codex-mini` 等
- 新規: `gpt54`, `gpt54-mini` → バージョン番号のドットを省略（既存慣例に準拠）

## 影響範囲
- ConfigMap変更のみ → chokidarホットリロードで自動反映（Pod再起動不要）
- 既存モデル定義・エイリアスに変更なし
- LiteLLMのbudget設定に影響なし（GPT-5.4はLiteLLMを通過しない）
- Secret/ExternalSecret変更不要
