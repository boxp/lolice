# BOXP-337: 現行 Gemma4 uncensored（26B-A4B）と同系列 31B 版の lolice 実機比較

## 目的

localllm の既定モデル `gemma4-26b` と、同じ作者・同じ系列の 31B 版を、同じ hardware / backend / quant / context で比べる。出力品質と速度・資源の実測を残し、31B の実用上の利点と費用（遅さ・資源）の証拠を出す。採用や既定の切替は比較の後に別途判断する。

## 現行と候補（確認済み）

| 項目 | 現行 `gemma4-26b` | 候補 `gemma4-31b` |
| --- | --- | --- |
| HF repo | `HauhauCS/Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-MTP` @ `f9093662` | `HauhauCS/Gemma4-31B-QAT-Uncensored-HauhauCS-Balanced-MTP` @ `9654466e` |
| file | `Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf`（16,796,015,520 bytes、sha256 `3c131334…`） | `Gemma4-31B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf`（18,687,062,176 bytes、sha256 `71667f9e…`） |
| 構造 | MoE（128 expert のうち 8 個、active は公称約 4B、総 25.2B）、30 層 | dense（active = 総 30.7B）、60 層 |
| license / base | gemma / `google/gemma-4-26B-A4B-it` | gemma / `google/gemma-4-31B-it` |

HauhauCS が公開している 31B 版はこの 1 つだけで、配布されている quant も Q4_K_M だけ。

## 変更内容

- `argoproj/local-llm/models-config.yaml` に `[gemma4-31b]` preset を追加する。batch / ubatch / threads は `gemma4-26b` と同じにし、違いはモデルファイルだけにする。
- 既定モデル、既存 preset、Deployment は変更しない。router は `--models-max 1` なので、31B は名指しの要求があったときだけロードされる。その間は 26B がアンロードされ、次に 26B の要求が来ると戻る。
- モデルファイルは golyat-4 の `/var/lib/local-llm/models/boxp337-gemma4-31b/` に置く。取得は revision 固定で、sha256 を HF LFS の値と照合する。codex-workspace の PVC にはコピーしない。

## 試験の手順

1. ファイルを取得して sha256 を照合する。その後この PR を merge し、Argo の sync と ConfigMap の readback、`GET /models` に `gemma4-31b` が出ることを確認する。
2. 安全区間で逐次 A/B を行う。localllm の進行中要求、Board の localllm 票、novel の自動投入がないことを確認してから始める。node で memwatch を動かし、available が 12 GiB を下回ったら `POST /models/unload` で 31B を外す。
3. 計測項目（両モデルとも同じ router、ctx 262144、thinking off、sampler は HauhauCS 推奨値で共通）
   - BOXP-336 の fixture 9 件 × 3 回: prose、code、8k long、JSON schema、tool call
   - 32k token の long context: cold 1 回と warm 1 回
   - BOXP-253 の bench と日本語回帰
   - BOXP-294 の A1 合成 fixture（灯火街）: T1 から T4
   - 実バースの A2: 本文は local model だけで生成し、Owner 用の Obsidian に保存する。cloud 側は metadata だけを見る
4. 試験の後、`gemma4-26b` を再ロードし、`/health` と runner の復帰を確認する。

## rollback

この PR を revert すると preset が消える。既定の経路には影響しない。取得したファイルの削除は Owner の判断に委ねる。

## 範囲外

既定モデルの切替、Strata の再試験、driver の変更、外部公開、既存モデルの削除、MTP（BOXP-253 で起動に失敗し、OOM のインシデントがあったため）。
