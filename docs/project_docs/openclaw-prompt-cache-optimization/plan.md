# OpenClaw Prompt Cache 最適化

## 概要

LiteLLM プロキシの `cache_control_injection_points` 設定を調整し、全モデルで会話履歴（過去メッセージ）に対する prompt cache が効く構成を実現する。

## 背景

### Anthropic Prompt Cache の仕組み

Anthropic の prompt cache は **prefix-based caching** を採用している:

1. `cache_control: {"type": "ephemeral"}` が付いたメッセージまでの **全プレフィックス** がキャッシュ対象
2. 後続リクエストでプレフィックスが一致すればキャッシュヒット（cache_read トークン）
3. キャッシュ TTL: 5分間（ephemeral）
4. 最大 4 ブレークポイント/リクエスト
5. 最小キャッシュサイズ: Sonnet/Opus = 1,024 トークン、Haiku = 2,048 トークン

### コスト構造（Anthropic）

| トークン種別 | Sonnet 4.5 | Opus 4.6 | 通常入力比 |
|---|---|---|---|
| 通常入力 | $3.00/MTok | $15.00/MTok | 1.0x |
| cache_write | $3.75/MTok | $18.75/MTok | 1.25x |
| cache_read | $0.30/MTok | $1.50/MTok | **0.1x** |

cache_read は通常入力の **10分の1** のコスト。長い会話履歴を持つ場合、大幅なコスト削減が期待できる。

## 変更前後の比較

### 変更前の構成

| モデル | cache_control_injection_points |
|---|---|
| anthropic/claude-sonnet-4-5 | system, -2, -1 |
| anthropic/claude-opus-4-6 | system, -2, -1 |
| zai/glm-4.7 | system, -2, -1 |
| zai/glm-4.5-flash | **(なし)** |
| google/gemini-2.5-flash | system のみ |
| google/gemini-2.5-pro | system のみ |
| openai/gpt-5-mini | (なし - 自動キャッシュ) |
| openai/gpt-5.1-codex-mini | (なし - 自動キャッシュ) |

### 変更後の構成

| モデル | cache_control_injection_points | 変更内容 |
|---|---|---|
| anthropic/claude-sonnet-4-5 | system, -2, -1 | 変更なし |
| anthropic/claude-opus-4-6 | system, -2, -1 | 変更なし |
| zai/glm-4.7 | system, -2, -1 | 変更なし |
| zai/glm-4.5-flash | **system, -2, -1** | **追加** |
| google/gemini-2.5-flash | **system, -1** | **-1 追加** |
| google/gemini-2.5-pro | **system, -1** | **-1 追加** |
| openai/gpt-5-mini | (なし) | 変更なし |
| openai/gpt-5.1-codex-mini | (なし) | 変更なし |

### Anthropic モデルが既に過去メッセージをキャッシュしている理由

現行の `system + -2 + -1` 構成は、prefix-based caching により **既に過去メッセージ全体をカバー** している:

1. ターン T: `[system] [msg1] ... [msg(T-2) ← BP] [msg(T-1) ← BP]`
   - キャッシュエントリ A: system ～ msg(T-2) のプレフィックス
   - キャッシュエントリ B: system ～ msg(T-1) のプレフィックス

2. ターン T+1: `[system] [msg1] ... [msg(T-2)] [msg(T-1) ← BP] [msg(T) ← BP]`
   - system ～ msg(T-2) のプレフィックスは前回のキャッシュエントリ B に含まれるためキャッシュヒット
   - 新しい msg(T-1), msg(T) のみが fresh input として処理される

つまり、**会話が伸びるほどキャッシュ効果が大きくなる**。

### Gemini / ZAI の改善点

- **Gemini**: `index: -1` を追加することで、system だけでなく会話全体のプレフィックスがキャッシュ対象に
  - LiteLLM の `drop_params: true` 設定により、Gemini API が非対応のパラメータは安全に無視される
- **ZAI glm-4.5-flash**: glm-4.7 と同等の設定を追加（同じプロバイダでの設定統一）

## 検証計画

### フェーズ 1: メトリクス確認（デプロイ直後）

OpenClaw の OTEL メトリクス `openclaw.tokens` で以下を確認:

```promql
# cache_read トークンの割合（モデル別）
sum(rate(openclaw_tokens_total{token_type="cache_read"}[5m])) by (model)
/
sum(rate(openclaw_tokens_total{token_type=~"input|cache_read|cache_write"}[5m])) by (model)
```

**期待値:**
- Anthropic モデル: cache_read 率 60-80%（長い会話）
- Gemini モデル: 変更後に cache_read 率が 0% → 20%以上に改善
- ZAI glm-4.5-flash: 変更後に cache_read が出現

### フェーズ 2: コスト比較（1週間後）

変更前後の 1 週間のコストを比較:

```promql
# 1 週間の総コスト推定（Anthropic）
sum(increase(openclaw_tokens_total{model=~"claude.*", token_type="input"}[7d])) * 3e-6
+ sum(increase(openclaw_tokens_total{model=~"claude.*", token_type="cache_write"}[7d])) * 3.75e-6
+ sum(increase(openclaw_tokens_total{model=~"claude.*", token_type="cache_read"}[7d])) * 0.3e-6
```

### フェーズ 3: レイテンシ比較（1週間後）

```promql
# API レスポンス時間（TTFT）
histogram_quantile(0.95, rate(openclaw_api_duration_seconds_bucket[5m]))
```

キャッシュヒット時は TTFT（Time To First Token）が改善する見込み。

### 採用可否判断基準

| 指標 | 採用基準 | 非採用/ロールバック基準 |
|---|---|---|
| cache_read 率 | > 30%（長い会話で） | 0% または微小な変化 |
| コスト変化 | 10%以上の削減 | コスト増加 |
| レイテンシ | 悪化なし | 明確な悪化（p95 +20%以上） |
| エラー率 | 変化なし | 新規エラーの発生 |

### メトリクスがまだ Prometheus に未到達の場合

OTEL メトリクスが Prometheus に到達していない場合は、LiteLLM のログから手動確認:

```bash
kubectl logs -n openclaw deployment/litellm | grep -i cache
```

## リスク評価

| リスク | 影響度 | 発生確率 | 対策 |
|---|---|---|---|
| Gemini が cache_control を無視 | 低 | 中 | `drop_params: true` により安全に無視。コスト影響なし |
| ZAI が cache_control を未対応 | 低 | 低 | 同上 |
| Anthropic 4 BP 上限超過 | 中 | 低 | 現行 3 BP のまま。追加なし |
| キャッシュ TTL 切れ | 低 | 中 | 5分以内の連続会話で効果あり。TTL は制御不可 |
| LiteLLM バージョン互換性 | 低 | 低 | LiteLLM は cache_control_injection_points を安定 API として提供 |

## ロールバック手順

1. `configmap-litellm.yaml` をこの PR 前の状態に戻す（git revert）
2. ArgoCD が自動同期で反映（または手動 sync）
3. LiteLLM Pod が ConfigMap 変更を検知して自動リロード

```bash
# 手動ロールバック
git revert <commit-hash>
git push origin main
# ArgoCD sync を待つか手動実行
argocd app sync openclaw
```

## 変更ファイル

- `argoproj/openclaw/configmap-litellm.yaml`: LiteLLM プロキシの cache_control_injection_points 設定調整
