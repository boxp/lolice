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

## 結果（2026-11-03、golyat-4、隔離 runtime strata-eval で実施）

### 確定した原因

- 停止は engine の GPU カーネル・量子化・OOM・i915 driver ではなく、engine が同梱する SYCL runtime
  （oneAPI DPC++ 2026.1.1 の Unified Runtime）の **Level Zero legacy adapter が既定の batched（非 immediate）
  command list を使う構成**で、Strata の prompt 経路（compute / copy の 2 本の in-order queue を
  `ext_oneapi_submit_barrier(event)` で相互依存させる）の event 同期が完了しなくなること。
  GPU は要求直後の約 20 秒だけ busy になった後 idle（周波数 0）になり、host の main thread が
  `m.cs->wait()` で spin し続けて 60 秒の自己監視で exit -6 になる。
- `SYCL_UR_USE_LEVEL_ZERO_V2=1`（Level Zero v2 adapter、intel/llvm の文書では Xe2 以降だけ既定）で同じ
  engine・同じ入力が完走する。legacy adapter のまま `UR_L0_USE_IMMEDIATE_COMMANDLISTS=1` でも完走する。
  engine 0.1.50（v0.2.6）/ 0.1.56（v0.2.10）とも同じ。prompt 長（25〜4357 token）は無関係。
- i915 の `Fence expiration time out` は red 実験の GPU busy 終了時だけ増え、green では増えない
  （同期不成立の結果で driver 障害ではない）。GPU reset は一度も起きていない。
- 一変数ずつ試して red のままだったもの: engine 側 env 14 種（PLE_BATCH / PROMPT_ATTN_OLD / SELECT_OLD /
  GR_UNFUSED / GDN_XE / PF_XMX / NO_LARGEPAGES / ARENA_LOCK / GROUP_COPY / WINDOW_SYNC / PREFILL_FIRST /
  PF_DQ_FUSED / PREFILL_STREAM_MIN / PREFILL_MMQ / PF_DMA_PAGEABLE）、`--prefill 512`、FP64 エミュ key 除去、
  `UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS`、legacy adapter の他 knob（IN_ORDER_BARRIER_BY_SIGNAL=0 /
  USE_COPY_ENGINE=0 / USE_DRIVER_INORDER_LISTS / SERIALIZE）。
- 未確定: legacy adapter の source（`urEventWait` / `createAndRetainUrZeEventList`）には他 queue の open
  command list を flush する処理があり、「現在 queue しか flush しない」仮説は一次資料で支持されなかった。
  legacy adapter のバグか Strata 側の queue 利用パターンの問題かは特定していない（上流 #5024 と同症状）。

### 実益比較（同一 GGUF GSQ-RCO Q2_0、temp 0、thinking off、BOXP-336 fixtures 9 件 × 3 反復）

| 指標 | Strata v0.2.6 + v2 adapter | llama.cpp b11669（BOXP-358 batch7 実測） |
| --- | --- | --- |
| 完了率 | 29/29 完走（watchdog 停止なし） | 27/27 |
| fixture pass | 21/29（code_2 0/3 は fixture 起因で共通、**tool_1 0/3**、prose_3 2/3） | 24/27（code_2 以外すべて pass、tool_1 3/3） |
| 短文 TTFT | 9〜13 s（prompt 8 tok/s） | 3〜5 s |
| 8.3k token prompt | 66 s（124〜130 tok/s） | 176 s cold（48 tok/s） |
| decode | 4.0〜4.2 tok/s | 5.8〜6.0 tok/s |
| 日本語創作 300 token | 76〜83 s | 49〜58 s |
| 出力品質 | prose 3/3 に中国語読点「，」11〜17 件、英単語混入（shimmering）、temp 0 でも反復ごとに本文が変わる | 「，」0、英語混入なし |
| tool 呼び出し | 5/5 で city="Tokyo"（fixture は 灯火街） | 3/3 正答 |
| RAM | engine RSS 33 GB、MemAvailable 最小 43.9 GB | child RSS 2.8〜13 GB、MemAvailable 最小 33.8 GB（ctx 262k 時） |

- 速度・品質ともに現状の llama.cpp を上回らない。長い prompt の prefill だけは Strata が約 2.7 倍速い。
- 結論: **現構成では採用しない**（原因は確定・修復済みだが実益がない）。strata-eval は replicas 0 に戻し、
  configmap に `SYCL_UR_USE_LEVEL_ZERO_V2=1` を残して再検証時に同じ停止を踏まないようにする。

### 復帰

- strata-eval replicas 0 / 通常 command、local-llm replicas 1（本 PR）。
- local-llm の実 scale と受付 gate は BOXP-349 の runtime file queue（条件付き PATCH）で scale 1 → open に戻す。
