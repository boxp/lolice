# OpenClaw GPTモデル統合計画（Azure OpenAI Service サブスクリプション利用）

## 概要

OpenClawが**Azure OpenAI Service**のサブスクリプション課金を通じてGPTモデル（gpt-4o-mini, o4-mini, codex-mini）を利用できるように、lolice cluster上のKubernetesマニフェストを修正する。

### レビュー指摘対応
> openaiのapi keyを使う時点でsubscriptionとは別の課金体系になってしまうので、このPRで求めているのはopenclawの別の設定によってsubscription課金で利用できるようにすることです。手動での操作も必要になるかもしれないのでそれも含めてまとめてください。また、最新のcodexモデルなどもモデルに含めてほしいです

**対応方針**:
- OpenAI API（従量課金）ではなく、Azure OpenAI Service（サブスクリプション課金）経由でモデルにアクセス
- LiteLLMの `azure/` プレフィックスを使用してAzure OpenAI Serviceにルーティング
- 最新のCodexモデル（o4-mini, codex-mini）を追加

## 変更内容

### 1. `configmap-litellm.yaml`
- `openai/gpt-4o-mini`: `azure/gpt-4o-mini` に変更（Azure OpenAI経由）
- `openai/o4-mini`: 新規追加（Azure OpenAI経由、推論モデル）
- `openai/codex-mini`: 新規追加（Azure OpenAI経由、Codexモデル）
- 認証: `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_API_BASE` を使用

### 2. `configmap-openclaw.yaml`
- `openai/gpt-4o-mini`: 名前を「GPT-4o mini (Azure)」に更新
- `openai/o4-mini`: 新規追加（reasoning: true, contextWindow: 200000）
- `openai/codex-mini`: 新規追加（reasoning: true, input: text only）
- fallbacksに `o4-mini` を追加
- エイリアス: `o4-mini`, `codex-mini` を追加
- `gpt-5.1-codex-mini` のエイリアスを `codex-5-mini` に変更（重複回避）

### 3. `external-secret-azure-openai.yaml` (新規)
- 既存の `litellm-credentials` Secretとは分離した `azure-openai-credentials` Secretを管理
- SSMパラメータ未登録時でも既存のLiteLLM Podに影響を与えない
- `AZURE_OPENAI_API_KEY`: Azure OpenAI APIキーをSSMから取得
- `AZURE_OPENAI_API_BASE`: Azure OpenAIエンドポイントURLをSSMから取得

### 4. `deployment-litellm.yaml`
- `AZURE_OPENAI_API_KEY` 環境変数を追加（`optional: true`）
- `AZURE_OPENAI_API_BASE` 環境変数を追加（`optional: true`）
- 参照先: `azure-openai-credentials` Secret（分離済み）
- Secret未作成時でもPodは正常起動（Azure OpenAIモデルのみ利用不可）

### 5. `kustomization.yaml`
- `external-secret-azure-openai.yaml` をリソースに追加

### 6. 変更不要な項目
- **NetworkPolicy**: 外部 HTTPS (443) アクセスは既に許可済み → Azure OpenAIエンドポイントへもアクセス可能

## 追加されるモデル

| モデル名 | 名前 | reasoning | 入力 | Context Window | Max Tokens |
|---------|------|-----------|------|---------------|------------|
| `openai/gpt-4o-mini` | GPT-4o mini (Azure) | false | text, image | 128,000 | 16,384 |
| `openai/o4-mini` | o4-mini (Azure) | true | text, image | 200,000 | 100,000 |
| `openai/codex-mini` | Codex mini (Azure) | true | text | 200,000 | 100,000 |

## 手動操作が必要な項目

### 1. Azure OpenAI Serviceリソースの作成

Azure Portalで以下を実施:

1. **Azure OpenAI Serviceリソースを作成**
   - リソースグループ: 既存または新規作成
   - リージョン: East US 2 または Japan East（モデル可用性に依存）
   - 料金プラン: Standard S0

2. **モデルのデプロイ**
   - Azure OpenAI Studio (`oai.azure.com`) にアクセス
   - 以下のモデルをデプロイ:
     - `gpt-4o-mini` (デプロイ名: `gpt-4o-mini`)
     - `o4-mini` (デプロイ名: `o4-mini`)
     - `codex-mini` (デプロイ名: `codex-mini`)
   - デプロイ名はLiteLLMの `model: "azure/{deployment-name}"` と一致させること

3. **APIキーとエンドポイントを取得**
   - Azure Portal → OpenAIリソース → Keys and Endpoint
   - KEY1 (or KEY2) をメモ
   - エンドポイント（例: `https://{resource-name}.openai.azure.com/`）をメモ

### 2. AWS SSM Parameter Storeへの登録

以下のSSMパラメータを手動で作成・更新:

```bash
# Azure OpenAI APIキー
aws ssm put-parameter \
  --name "/lolice/openclaw/AZURE_OPENAI_API_KEY" \
  --type "SecureString" \
  --value "<Azure OpenAI API Key>" \
  --overwrite

# Azure OpenAI エンドポイント
aws ssm put-parameter \
  --name "/lolice/openclaw/AZURE_OPENAI_API_BASE" \
  --type "SecureString" \
  --value "https://<resource-name>.openai.azure.com/" \
  --overwrite
```

### 3. Terraform SSM定義の追加（boxp/arch）

`terraform/aws/openclaw/ssm.tf` に以下を追加（次のPRで対応推奨）:

```terraform
resource "aws_ssm_parameter" "azure_openai_api_key" {
  name        = "/lolice/openclaw/AZURE_OPENAI_API_KEY"
  description = "Azure OpenAI API Key for subscription-based model access"
  type        = "SecureString"
  value       = "dummy-value-to-be-updated-manually"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "azure_openai_api_base" {
  name        = "/lolice/openclaw/AZURE_OPENAI_API_BASE"
  description = "Azure OpenAI endpoint URL"
  type        = "SecureString"
  value       = "dummy-value-to-be-updated-manually"
  lifecycle {
    ignore_changes = [value]
  }
}
```

### 4. ArgoCD同期

マニフェスト適用後、ArgoCDで手動同期が必要になる場合がある:
- ExternalSecretが新しいSSMパラメータを取得するまで最大1時間の遅延
- 即時反映が必要な場合: ExternalSecretを手動で再同期

## リスク・注意点

- Azure OpenAI Serviceの契約が必要（Enterpriseまたはstandard subscription）
- モデルの可用性はリージョンに依存（o4-mini, codex-miniは一部リージョンのみ）
- LiteLLMの `api_version: "2025-01-01-preview"` は最新版を使用すること
- LiteLLMの `drop_params: true` 設定により、Azure OpenAIが対応しないパラメータは自動的に無視される
- 月間予算 $50 / 30日の制限は全モデルで共有（LiteLLM側で制御）
- 既存の `OPENAI_API_KEY`（OpenAI API直接利用）は gpt-5-mini, gpt-5.1-codex-mini で引き続き使用
