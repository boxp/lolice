# OpenClaw Fallback Vertex Cache Fix

## 概要

OpenClaw の fallback で使っている `litellm/google/gemini-2.5-flash` が、Vertex AI の `CachedContent` 制約により `HTTP 400` を返していたため、Gemini 系モデルへの `cache_control_injection_points` を削除する。加えて、Google AI Studio 用の `api_base` を明示し、意図しない provider 解決を避ける。

## 発生していたエラー

```text
CachedContent can not be used with GenerateContent request setting system_instruction, tools or tool_config.
```

## 原因

- LiteLLM の `cache_control_injection_points` により Gemini リクエストで prompt cache が有効化される
- Vertex AI の `CachedContent` は、同じ `GenerateContent` リクエスト内に `system_instruction` / `tools` / `tool_config` があると `INVALID_ARGUMENT` を返す
- OpenClaw の fallback 呼び出しでは system prompt や tools を併用するため、Gemini fallback で衝突する
- LiteLLM の Gemini 実装は Vertex AI と Google AI Studio の共通コードパスを通るため、endpoint を明示しないと挙動の切り分けがしづらい

## 変更内容

### `argoproj/openclaw/configmap-litellm.yaml`

- `google/gemini-2.5-flash` の `cache_control_injection_points` を削除
- `google/gemini-2.5-pro` の `cache_control_injection_points` を削除
- `google/gemini-2.5-flash` / `google/gemini-2.5-pro` に `api_base: https://generativelanguage.googleapis.com` を追加
- Anthropic / ZAI / OpenAI 系の設定は変更しない

## 期待効果

- OpenClaw fallback で Gemini 2.5 Flash を使った際に `HTTP 400` が発生しなくなる
- Vertex AI と衝突する Gemini 向け prompt cache のみ無効化される
- Gemini リクエストの接続先を Google AI Studio endpoint に固定できる
- 他モデルのキャッシュ戦略と fallback 順序は維持される

## 検証

1. OpenClaw で primary を失敗させ、`litellm/google/gemini-2.5-flash` fallback を発生させる
2. 以前の `CachedContent ... system_instruction, tools or tool_config` エラーが消えることを確認する
3. LiteLLM debug log などで Gemini の送信先が `generativelanguage.googleapis.com` になっていることを確認する
4. Anthropic / ZAI モデルで既存の prompt cache が引き続き有効なことを必要に応じて確認する

## ロールバック

- `configmap-litellm.yaml` の Gemini 向け `cache_control_injection_points` を元に戻す
- ArgoCD sync 後に LiteLLM が再読込されることを確認する
