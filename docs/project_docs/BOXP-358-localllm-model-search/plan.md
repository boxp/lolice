# BOXP-358: lolice の local LLM 候補の継続比較

## 目的

lolice 上で実用最高性能の local LLM を継続的に比較する。品質、速度、資源の実測を残し、採用や既定モデルの切替は比較後に別途判断する。

## 変更内容

- この PR は `qwen36-35b-a3b`、`ornith15-35b-a3b`、`qwen35-122b-a10b`（最大級の載せられる候補、UD-Q2_K_XL 41.85 GB）の選択可能な preset だけを追加する。
- 既定モデルと Deployment は変更しない。router の `--models-max 1` により、候補は名前を指定した要求でのみロードされる。
- モデルの取得先は `/var/lib/local-llm/models/boxp358/` とし、固定 revision と sha256 を照合する。
- sampler は呼び出し側が指定する。context などは router の設定を使う。

## 資源と時間の上限

- 候補 1 つの weights + KV + buffer は 55 GB 以下とする。
- `MemAvailable` が 12 GiB を下回ったら候補を unload する。
- disk は 800 GB を確保し、本票のダウンロードは合計 150 GB までとする。
- 1 候補の試験は 90 分を上限とする。

## 試験の手順

1. 候補を取得し、revision と sha256 を確認する。
2. router 経由で BOXP-337 と同じ suite（load / F / L32 / B253 / A1 / A2）を実行する。
3. 品質、速度、資源の結果を記録し、候補を逐次比較する。
4. 試験後は既定モデルを再ロードし、正常な復帰を確認する。

## rollback

この PR を revert して preset を削除する。取得済みの `/models/boxp358/<dir>` は Owner の判断後に削除する。

## batch 2（追加 preset）

batch 1（gemma4-26b baseline、ornith-35b、qwen36-35b-a3b、ornith15-35b-a3b、qwen35-122b-a10b）の結果を受けて、次の 2 候補を追加する。

- `qwen-agentworld-35b-a3b`: unsloth/Qwen-AgentWorld-35B-A3B-GGUF @ `3a305abf`、UD-Q4_K_M 22.13 GB、apache-2.0。カードが環境シミュレーション（世界設定）用途を明記している。
- `nemotron35-lightning-30b-a3b`: ggml-org/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF @ `8cd08217`、Q4_0 18.90 GB、openmdw-1.1。対応言語に日本語を明記。b11669 で `nemotron_h_moe` は登録済み。attention 層が 6 層のため、262144 ctx でも KV は約 1.6 GB。

ダウンロード累計は約 123 GB で、150 GB の上限内に収まる。suite には coding phase（`C`: 合成 Clojure 5 課題 × 2）を追加した。

## batch 3（coding 軸の追加 preset）

Owner 補足（coding 用 local LLM は NSFW 用途に限らない）を受け、coding 軸の候補として追加する。

- `qwen3-coder-30b-a3b`: unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF @ `b17cb02d`、Q4_K_M 18.56 GB、sha256 `fadc3e5f…`、apache-2.0。262144 ctx で KV は約 25.8 GB、weights + KV + buffers は約 47 GB で候補ごとの上限 55 GB 以下。

- `nemotron-nano-9b-v2-ja`: mmnga-o/NVIDIA-Nemotron-Nano-9B-v2-Japanese-gguf @ `309f87c4`、Q4_K_M 6.53 GB、sha256 `2e4e0d47…`、nvidia-open-model-license。出版社が日本語最適化を明記している小型 dense の候補。
- `qwen-agentworld-35b-a3b` を退役する。日本語回帰が 12/18（英語混入）で全候補の最下位、同系統（qwen35moe）の qwen36-35b-a3b を上回る指標がない。本票で取得した weights（22.13 GB）は preset 削除の反映後に消す。revision を固定しているため再取得できる。

既定 model は不変。退役後のダウンロード保持量は約 131 GB で、150 GB の上限内に収まる。

## batch 4（大型 coding MoE の追加と退役）

coding 軸の大型 MoE 候補として `qwen3-coder-next` を追加する。unsloth/Qwen3-Coder-Next-GGUF @ `ce09c67b`、Q4_K_S 45.53 GB、sha256 `95e490dd…`、apache-2.0。262144 ctx の f16 KV は約 6.4 GB、weights + KV + buffers は約 54.47 GB で候補ごとの上限 55 GB 以下。既定 model は不変。

- `qwen36-35b-a3b` を退役する。同系統の `ornith15-35b-a3b` は coding 10/10 対 8/10、速度も同等以上で、上回る指標がない。
- `nemotron35-lightning-30b-a3b` を退役する。coding 6/10、日本語回帰 14/16、8k needle 未回答 2 件で最下位。

両者の weights は revision 固定で再取得できる。本票のダウンロード保持量は退役後に 150 GB の上限内に収める。

## batch 5（g26 の MTP 投機的デコード、速度の独立実験）

本線の比較が動いた後の optional な高速化実験として、`gemma4-26b-mtp` を追加する。本体は既定の `gemma4-26b` と同じ weights（HauhauCS の MTP 配布物の Q4_K_M と sha256 `3c131334…` で一致を確認）で、draft head `mtp-gemma-4-26B-A4B-it.gguf`（revision `f9093662`、252 MB、sha256 `62bd3af7…`、license gemma）だけを本票の model store に追加した。llama.cpp b11669 は gemma4 の `draft-mtp`（target の KV を共有）を実装している。投機的デコードは draft の token をすべて target で検証するため、出力は `gemma4-26b` と同じになる想定で、この preset では速度だけを比べる。既定 model と router 設定は変えない。

- 計測: load、F（temp 0 で `gemma4-26b` と出力が一致するか）、L32、B253（pp / tg）、A1。受理率（draft_n / draft_n_accepted）も記録する。
- 起動に失敗した場合や速度が上がらない場合は、理由を記録して preset を外す。

batch 5 の結果: 日本語散文では draft の受理率が 33〜40% にとどまり、decode が 15.2 → 10.8 t/s（約 -29%）、日本語回帰の平均 tg も 13.5 → 9.9 t/s に下がった。code / json は同等〜 +15%。創作が主用途のため不採用とし、`gemma4-26b-mtp` preset を外す。draft head は preset 撤去の反映後に model store から消す。

## batch 6（Ornith-1.5 uncensored 派生の追加と退役）

Owner が実バース出力で ornith15 / q36 を g26 より読みやすいと評価し、ornith15 の uncensored 版の比較を求めたため、`ornith15-35b-a3b-uncensored` を追加する。junafinity/Ornith-1.5-35B-A3B-uncensored-GGUF-8bit @ `0cab5bc6`、Q8_0 37.80 GB、sha256 `d0cd5a65…`。第三者による abliteration 派生で Ornith 公式ではなく、card 表記は apache-2.0、base 表記は mit。KV は約 5.4 GB、合計見積は約 45 GB で候補ごとの上限 55 GB 以下。既定 model は不変。

同じ派生の Q4_K_M は公開されていないため、ornith15（公式 Q4_K_M）との比較には量子化差（Q8_0 vs Q4_K_M）が交絡する。評価は一般的な非性的創作と指示追従で行い、NSFW 適性は評価しない。

- `qwen3-coder-30b-a3b` を退役する。coding で qwen3-coder-next に劣後し、CH は 0/9 対 6/9。
- `nemotron-nano-9b-v2-ja` を退役する。全軸で劣後する。

両者の weights は削除済みで、revision 固定で再取得できる。本票の保持量は追加後も 150 GB の上限内に収まる。
