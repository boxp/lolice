# T-20260305-010: OpenClawモデル選択にGPT-5.4系を追加

## 目的
boxp/lolice上のOpenClawでGPT-5.4系モデルを選択できるようにし、運用時にモデル切替を可能にする。

## 変更対象ファイル

### 1. `argoproj/openclaw/configmap-litellm.yaml`
- LiteLLMプロキシに `openai/gpt-5.4` と `openai/gpt-5.4-mini` のルーティング定義を追加
- 既存の `OPENAI_API_KEY` 環境変数を使用（追加のSecret設定不要）

### 2. `argoproj/openclaw/configmap-openclaw.yaml`
- `models.providers.litellm.models[]` に GPT-5.4 / GPT-5.4 mini のモデル定義を追加
  - reasoning: true, input: text+image, contextWindow: 400k, maxTokens: 128k
- `agents.defaults.models` にエイリアスを追加
  - `litellm/openai/gpt-5.4` → alias: `gpt54`
  - `litellm/openai/gpt-5.4-mini` → alias: `gpt54-mini`

## 設計判断

### プロバイダー選択: LiteLLM経由
- 既存の `openai/gpt-5-mini`, `openai/gpt-5.1-codex-mini` と同じくLiteLLM経由で提供
- OpenAI APIキーは既にExternalSecretで管理済みのため追加設定不要
- ホットリロード対応（Pod再起動不要で設定反映可能）

### モデルバリエーション
- `gpt-5.4`: 標準モデル（マルチモーダル対応）
- `gpt-5.4-mini`: 軽量・高速バリアント（既存のmini命名規約に準拠）
- タスクで言及されていた `gpt-5.4-pro` については、既存のOpenAI命名規約にmini/standardパターンが使われているため、miniバリアントを採用

### エイリアス命名
- 既存パターン: `gpt5-mini`, `gpt4o-mini` → ハイフン区切り
- 新規: `gpt54`, `gpt54-mini` → バージョン番号のドットを省略（既存慣例に準拠）

## 影響範囲
- ConfigMap変更のみ → chokidarホットリロードで自動反映（Pod再起動不要）
- 既存モデル定義・エイリアスに変更なし（追加のみ）
- Secret/ExternalSecret変更不要
