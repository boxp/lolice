# OpenClaw GPTモデル統合計画（OpenAI Codex Subscription利用）

## 概要

OpenClawが**OpenAI Codex サブスクリプション**（ChatGPT/Codex subscription）を通じてGPTモデルを利用できるように、lolice cluster上のKubernetesマニフェストを修正する。

### レビュー指摘対応

> azureAIのsubscriptionは課金していないので使えません。openaiのサブスクリプションを利用したいのです
> https://docs.openclaw.ai/providers/openai#option-b-openai-code-codex-subscription

**対応方針**:
- Azure OpenAI Service（未契約）ではなく、**OpenAI Codex Subscription** (OAuth認証) を使用
- LiteLLM経由ではなく、OpenClawのネイティブ `openai-codex` プロバイダーを直接使用
- 認証: ChatGPT OAuth (PKCE) で自動管理（API Keyは不要）
- 参考: [OpenClaw Docs - Option B: OpenAI Code (Codex) Subscription](https://docs.openclaw.ai/providers/openai#option-b-openai-code-codex-subscription)

## 変更内容

### 1. `configmap-openclaw.yaml`
- `openai-codex` プロバイダーを新規追加
  - `api`: `"openai-codex"` （OpenClawネイティブプロバイダー）
  - モデル: gpt-4o-mini, o4-mini, gpt-5.3-codex
- fallbacksに `openai-codex/gpt-4o-mini`, `openai-codex/o4-mini` を追加
- エイリアス: `gpt4o-mini`, `o4-mini`, `codex` を追加
- LiteLLM内のAzure OpenAIモデル定義（gpt-4o-mini, o4-mini, codex-mini）を削除

### 2. `configmap-litellm.yaml`
- Azure OpenAI経由のモデル定義を削除（gpt-4o-mini, o4-mini, codex-mini）
- 既存のOpenAI API Key利用モデル（gpt-5-mini, gpt-5.1-codex-mini）は維持

### 3. `deployment-litellm.yaml`
- Azure OpenAI環境変数（AZURE_OPENAI_API_KEY, AZURE_OPENAI_API_BASE）を削除
- 既存のOPENAI_API_KEY（litellm-credentialsから）は維持

### 4. `external-secret-azure-openai.yaml` (削除)
- Azure OpenAI用のExternalSecretは不要のため削除

### 5. `kustomization.yaml`
- `external-secret-azure-openai.yaml` の参照を削除

## 追加されるモデル（openai-codexプロバイダー）

| モデルID | 名前 | reasoning | 入力 | Context Window | Max Tokens | エイリアス |
|---------|------|-----------|------|---------------|------------|----------|
| `gpt-4o-mini` | GPT-4o mini | false | text, image | 128,000 | 16,384 | gpt4o-mini |
| `o4-mini` | o4-mini | true | text, image | 200,000 | 100,000 | o4-mini |
| `gpt-5.3-codex` | GPT-5.3 Codex | true | text | 400,000 | 128,000 | codex |

## 手動操作が必要な項目

### 1. OpenAI Codex OAuthログイン

OpenClawのPodまたはCLIから、サブスクリプションのOAuth認証を実施:

```bash
# オンボーディングウィザードでCodex OAuthを選択
openclaw onboard --auth-choice openai-codex

# または直接OAuthログインを実行
openclaw models auth login --provider openai-codex
```

これにより:
1. PKCE verifier/challengeとランダムstateを生成
2. `https://auth.openai.com/oauth/authorize?...` がブラウザで開く
3. コールバックでトークンを取得
4. `auth-profiles.json` にアクセストークン・リフレッシュトークンを保存

ヘッドレス環境（K8s Pod内）の場合:
- リダイレクトURL/codeを手動ペーストする方式が利用可能
- トークンは `~/.openclaw/agents/<agentId>/agent/auth-profiles.json` に保存
- トークンの有効期限管理と自動リフレッシュはOpenClawが自動実行

### 2. ArgoCD同期

マニフェスト適用後にArgoCDで同期:
- Azure OpenAI関連リソースの削除が含まれるため、Pruneオプションを有効にする必要がある場合あり

## 既存モデル（LiteLLM経由、変更なし）

| プロバイダー | モデルID | 認証方式 |
|------------|---------|---------|
| litellm | anthropic/claude-sonnet-4-5-20250929 | ANTHROPIC_API_KEY |
| litellm | anthropic/claude-opus-4-6 | ANTHROPIC_API_KEY |
| litellm | zai/glm-4.7 | ZAI_API_KEY |
| litellm | zai/glm-4.5-flash | ZAI_API_KEY |
| litellm | google/gemini-2.5-flash | GEMINI_API_KEY |
| litellm | google/gemini-2.5-pro | GEMINI_API_KEY |
| litellm | openai/gpt-5-mini | OPENAI_API_KEY |
| litellm | openai/gpt-5.1-codex-mini | OPENAI_API_KEY |

## リスク・注意点

- OpenAI Codex Subscriptionの有効なアカウントが必要（ChatGPT Plus/Pro等）
- 初回OAuth認証はブラウザが必要（ヘッドレス環境では手動トークンペースト）
- トークンは自動リフレッシュされるが、サブスクリプションが失効すると利用不可
- LiteLLMの月間予算 $50/30日制限は openai-codex プロバイダーには適用されない（LiteLLMを経由しないため）
- モデルの利用可能状況はOpenAIのサブスクリプションプランに依存
