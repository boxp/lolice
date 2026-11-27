# BOXP-746: Hy-MT3-30B-A3B Q4_K_M を llama-server の切替用 preset に追加する

## 背景

TD-01 で、Gemma 4 と同じ live llama-server（image llama-sycl b11669、SYCL0、golyat-4）で Hy-MT3 Q4_K_M を実測した。生成は 11.7〜12.5 tok/s（gemma4-26b は 13.2〜13.7 tok/s）で、HANG は 0 だった。Owner は「①切替用プリセットを追加する」を選んだ。Gemma 4 は既定のまま残し、Hy-MT3 は翻訳用に名指しで選ぶ。

## 変更

- `argoproj/local-llm/models-config.yaml` に `[hy-mt3-30b-a3b]` を追加する。
  - model: `/models/boxp746/Hy-MT3-30B-A3B-Q4_K_M.gguf`（TD-01 で取得し sha256 を照合した weights。新規取得はしない）
  - batch 2048 / ubatch 2048 / threads 14（TD-01 と同じ値）。sampler は呼び出し側が渡す。
- 既定モデル・既存 preset・Deployment・envoy・admission は変えない。`--models-max 1` のままなので gemma4-26b と同時には載らない。名指しの要求があったときだけ入れ替わる。

## 資源の見積もり

hy_v3 は 48 層、KV head 4 × 128。f16 KV は router の ctx 262144 で約 24 GiB になる。weights と buffer（TD-01、ctx 32768 で GPU 共有 23.0 GiB）と合わせて約 44 GiB。BOXP-358 の候補ごとの上限 55 GB に収まる。gemma を unload したときの MemAvailable は約 51 GiB。初回ロードは memwatch で監視し、12 GiB を下回ったら unload する。

## 反映と確認

1. 受付を閉じる（rtapi `gate closed:BOXP-746-…`）→ drain（inflight 0）。
2. PR を merge → Argo sync → ConfigMap を readback → reloader で pod が再起動 → `/v1/models` に `hy-mt3-30b-a3b` が出ることを確認する。
3. 受付を閉じたまま、コンテナ内の 8081 で `hy-mt3-30b-a3b` に翻訳を要求し、実翻訳を確認する。MemAvailable と HANG も見る。
4. `gemma4-26b` を要求して切り戻し、実推論（「2+3」→「5」）を確認する。
5. 受付を開け、通常経路で health 200 と gemma の実推論を確認する。

## rollback

この PR を revert すると preset が消える。既定の経路には影響しない。

## 仕様・Lean について

設定 1 項目の追加のみで、状態遷移は既存 router（models-max=1）の挙動そのもの。新しい規則がないため、Lean モデルは作らない。実機確認で代える。

## 範囲外

既定モデルの変更、weights の追加取得や別量子化、外部公開、新規課金、ゲームへの反映。

## 追補: ctx を 32768 に制限する（PR #1454 反映後）

PR #1454 を受付閉で反映し、preset から 8 例中 7 例の実翻訳を確認した（12.0〜12.7 tok/s、置換記号は保持）。ただし router の ctx 262144 のままでは、ロード後に MemAvailable が 54 GiB から 5.8 GiB まで下がった（CD-01 の 12 GiB 基準を下回る）。すぐ unload して gemma の実推論「5」と受付 open まで戻した。HANG は 0。

見積り（KV 約 24 GiB）より実消費がずっと大きかったため、preset に `ctx-size = 32768` を足す。これは TD-01 で実測した構成（GPU 共有 23.0 GiB）と同じ。翻訳の入力は短いので 32768 で足りる。

反映後の確認は前と同じ手順（受付閉→drain→merge/sync→hy-mt3 実翻訳と MemAvailable 監視→gemma 実推論→受付 open）。

仕様・Lean: 設定値 1 行の変更で新しい規則はないため作らない。実機確認で代える。

## 追補 2: ルーター CLI の `-c` を外す（PR #1455 反映後）

PR #1455 を受付閉で反映したが、hy-mt3 の子プロセスは `--ctx-size 262144` で起動した（実機ログ）。b11669 ではルーターの CLI 引数が preset の値より優先されるため、deployment の `-c 262144` が preset の `ctx-size = 32768` を上書きしていた。確保が進む前に unload し、gemma の実推論「5」と受付 open まで戻した（MemAvailable 最低 26 GiB、HANG 0）。

対処: deployment から `-c 262144` を外し、ctx-size を持たない全 preset に `ctx-size = 262144` を明示する。各モデルが今まで実際に動いていた ctx を変えないため、ornith-35b の `65536`（これまで一度も効いていなかった値）も 262144 にそろえる。hy-mt3 だけ 32768 にする。

確認: 受付閉→drain→merge/sync→gemma の子プロセス引数が `--ctx-size 262144`、hy-mt3 が `32768` であることをログで確認→hy-mt3 実翻訳と MemAvailable 監視→gemma 実推論→受付 open。

仕様・Lean: 設定値の変更で新しい規則はないため作らない。実機の起動引数とメモリ監視で代える。
