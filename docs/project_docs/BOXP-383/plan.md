# BOXP-383: Strata 再挑戦（Arc 140T / i915 の GPU 待ち・自己 abort の原因究明と隔離修復）

チケット: BOXP-383（継続元 BOXP-336。336 の結果は書き換えない）
正本ノート: Obsidian `Projects/strata-localllm-evaluation/boxp383-retry.md`（本票の入口）

## 前提（調査で確認したこと）

- 旧 336 の停止は全て `reading the prompt (batched)` の layer 0/1、chunk `from token 0` で発生し、
  `0 layers served`・expert pool 全 worker sleeping。短い prompt（≤64 token、`--short-read` 既定）は
  verify window 経路なので batched 経路を通らない。
- engine 0.1.50 の SYCL 版では `cudaHostRegister` は no-op（dpct 置換）。expert arena は pageable。
  0.1.48 で A750 の prompt deadlock（pageable からの直接 DMA、Level Zero 内で spin・GPU idle）を
  stager 経由に変えた回避が入っている（`sycl/src/prefill/prefill.cpp:111`）。
- 上流最新 v0.2.10（engine 0.1.56、2026-11-02）。同じ Arc 140T（xe driver）で同症状の open issue #5024、
  Meteor Lake iGPU の #2282（4 GiB alloc 上限、window 停止、`STRATA_WINDOW_SYNC=1`）がある。
- engine source は node 保存の `src-4b9e05a2/sycl/` に同梱、`sycl/tools/Dockerfile` で再ビルド可能。
- 診断用 env: `STRATA_PF_STEP_SYNC=1`（step 単位で同期し停止 step を名指し）、`STRATA_PREFILL_SYNC`、
  `STRATA_PREFILL_TIMING`、`--short-read 0`（短い prompt でも batched 経路）、`STRATA_WATCHDOG_S`。

## 実施手順

1. **隔離 runtime の準備（GitOps、既存 strata-eval を再利用）**
   - BOXP-358 の GPU 使用（Flash-Next suite）が終わり、router が idle であることを確認してから
     PR: `local-llm` replicas 0、`strata-eval/strata-server` replicas 1（診断モード: `sleep infinity`、
     engine は `kubectl exec` から一変数ずつ起動）。AppProject/Namespace は変更しない。
   - node 側 observer（intel_gpu_top、dmesg Fence 件数、engine thread 状態）を `/tmp/boxp383-logs` に記録。
2. **最小反例と二分**
   - 旧設定で red を再現 → `--short-read 0` で prompt 長を 8/32/64/128/512 と変え境界を取る。
   - `STRATA_PF_STEP_SYNC=1` で停止 step を名指し。GPU busy/idle と Fence の時刻を対応付ける。
   - 一変数ずつ: FP64 エミュ key 有無、`STRATA_PLE_BATCH`、`STRATA_PROMPT_ATTN_OLD`、`STRATA_SELECT_OLD`、
     `STRATA_GR_UNFUSED`、`STRATA_GDN_XE`、`STRATA_PF_XMX`、`STRATA_NO_LARGEPAGES`、`STRATA_ARENA_LOCK=0`、
     `STRATA_GROUP_COPY=1`、`UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS=1`、`--prefill` chunk。
3. **上流版の評価**: engine 0.1.56（SHA256SUMS 照合）+ 同 tag の `serve/` で同じ最小反例を再試験。
4. **修復**: 設定回避で green になれば GitOps の ConfigMap に反映。source 修正が要る場合は
   `strata-sycl-dev` image で最小 patch を build し隔離 runtime で red→green を実証（timeout 延長や
   watchdog 無効化だけで成功扱いにしない）。
5. **実益比較**: 回復後に 336 の fixtures（短文/長文/JSON/tools）を同一モデル・同一 budget で Strata と
   llama.cpp（358 batch7 の実測を baseline に再利用）で比較。cold/warm・thinking off を区別。
6. **復帰**: PR で local-llm replicas 1、strata 0 に戻し、router `/health` と既定モデル（gemma4-26b 創作 /
   Qwen3-Coder-Next coder）の readback。
7. **記録**: Obsidian 入口ノートに再現手順・最小反例・仮説採否・実測・残る不明点を保存。

## 安全境界

- 他 run の強制中断なし。localllm の停止は inflight 自然完了を確認した限定試験に限る。
- kernel/driver 変更・node 再起動・既定モデル変更・データ削除・外部送信（上流 issue 投稿を含む）はしない。
- MemAvailable < 12 GiB で即中止・通常構成復帰。同一失敗の blind retry はしない。
