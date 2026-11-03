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

## 2026-11-03 追加試験（Owner 方針の再明示を受けた有限 batch5）と運用 project の整理

Owner「Strata の検証時は local-llm を停止する。最終的には cluster 上で新しく project を作って運用したい」。
hermes-agent 08:59 UTC の方針に従い、read-only 追補（run 084414Z）で「未確定」とした 2 点だけを実 GPU で切分ける。
追加の高精度 weights DL・pack 生成・driver 変更はしない。

### 手順（前回 #988 / #993 と同じ正規手順）

1. 本 PR: `local-llm` replicas 0（git 上の記録。実 scale は BOXP-349 の runtime file queue）、`strata-eval/strata-server`
   replicas 1 + `sleep infinity`（診断モード）。AppProject / Namespace / ConfigMap は変更しない。
2. merge 前に受付 gate を `closed:<run token>` にし、envoy の `cluster.llama_server.upstream_rq_active` と `pending_active` が 0
   （inflight 自然完了）を read back してから llama-server を scale 0 → merge → strata-server Pod Running を確認。
3. Pod 内で一変数ずつ（engine 0.1.50 + `SYCL_UR_USE_LEVEL_ZERO_V2=1`、Q2_0、temp 0、thinking off、fixtures prose_1 / tool_1）:
   - A1: 既定設定 + `STRATA_DECODE_TIMING=1`（request 単位の decode 内訳: window 数、ms/window、speculative / rollback）× prose_1 3 本
   - A2: A1 + `STRATA_VERIFY_PROFILE=1`（window 内の GPU stage の ms。profile 中は sh_stream の fork が無効になるため A1 と分ける）× prose_1 2 本
   - B1: `--expert-cache 1`（expert を全て CPU で計算 = GPU/CPU の丸め差を消す）× prose_1 3 本 + tool_1 2 本
   - B2: `--kv fp16`（KV int8 の影響）× prose_1 3 本 + tool_1 2 本
   - B3: `STRATA_ROUTE_TAIL_SKIP` は engine 0.1.50 / 0.1.56 の source に存在しない（docs main の記述は後続 engine のもの）ため実施しない。
   - template: pack の `chat_template.jinja` と GGUF 埋め込み template の差分、tool 定義の描画差を read-only で比較する。
   各要求は「，」件数・英単語混入・tool 呼び出し引数・decode tok/s・TTFT を記録する。
4. 終了: engine/server 停止 → node 上の strata process 0 を確認 → 復帰 PR（strata 0 / 通常 command、local-llm 1、結果を本 plan.md へ）
   → scale 1 → gate `open:<token>` → router `/health` と gemma4-26b の実推論を read back。
5. 中止条件: MemAvailable < 12 GiB、watchdog 停止の再発、node の他 workload 影響。即座に 4. へ。

### 既存 strata-eval の役割と「運用 project」の整理（設計のみ、本 PR では作らない）

- 既存（BOXP-336 で作成済み）: AppProject `strata-eval`（sourceRepos lolice、destination namespace `strata-eval` のみ、
  cluster-scoped 不可、namespace 内は ConfigMap / Deployment のみ）、Application `strata-eval`（automated prune / selfHeal）、
  Namespace は親 `argocd-apps` が作る。Deployment `strata-server` は replicas 0、hostPath `/var/lib/local-llm/models/boxp336-strata`
  を read-only で mount、Service なし（Pod 内 loopback のみ）、`gpu.intel.com/i915: 1` を要求するため llama-server と同時に
  schedule されない。**役割 = 隔離試験専用**（診断モードと通常 command を PR で切り替える）。
- Owner の「新しい project」は Argo CD の AppProject を指す。`strata-eval` は既に専用 AppProject なので、運用用に
  もう一つ作る価値があるのは「常時サービスとして入口・監視・停止復帰を持つ」段階に進む時だけ。
- 運用 project 案（`strata`）: Namespace `strata`、AppProject `strata`（whitelist に Service / ServiceMonitor / NetworkPolicy を追加）、
  Application `strata`（`argoproj/strata/`、base は strata-eval の manifests を kustomize で参照して重複を作らない）、
  Deployment `strata-server`（ClusterIP Service 9137、readiness は `/health`、weights は `/var/lib/local-llm/models/strata/<pack>`
  を read-only hostPath で保全、logs は emptyDir）、GPU 排他は local-llm の admission gate（closed → drain → scale 0）と
  i915 resource 1 個の二重で担保、利用入口は LAN Service ではなく router 経由を検討、停止復帰は replicas の条件付き PATCH、
  更新 rollback は Argo の revision revert。
- 採用条件（これを満たすまで運用 project は作らない）: 同一 fixtures で llama.cpp と同等以上の品質（tool 3/3、「，」0）、
  かつ速度または容量（IQ3_S 級が載る等）で実測の利点があること。Q2_0 の今回結果はどちらも未達。

### batch5 結果（2026-11-03 09:13〜09:30 UTC、engine 0.1.50 + `SYCL_UR_USE_LEVEL_ZERO_V2=1`、Q2_0、temp 0、thinking off）

local-llm は gate `closed:BOXP-383-20261103T085957Z` → drain → scale 0 の正規手順で停止（#994）。pod 内で実験ごとに engine を
起動し直し、node observer（intel_gpu_top / Fence 件数 / thread 状態）を併走。watchdog 停止は a2 の 1 件のみ、i915 Fence 件数は
3110 のまま増加なし、MemAvailable 最小 46 GB。全ログは vault `artifacts/boxp383/logs/batch5/`。

| 実験 | 条件 | 反復 | 完走 | decode tok/s | prompt ms (rep0 / cached) | 「，」件数 | 英単語混入 | tool 引数 | 備考 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| a1 | 既定 + DECODE_TIMING, prose_1 | 3 | 3/3 | 3.9 / 4.7 / 4.1 | 18322 / 756 | 0 / 0 / 0 | 0 / 117 / 0 | — | rep1 は `<think>` を出力して max_tokens 800 到達（thinking off 指定なのに思考漏れ）。簡体字語彙（水洼・包裹・羁绊）は 3 本とも |
| a2 | a1 + VERIFY_PROFILE + VERIFY_EAGER, prose_1 | 1 | 0/1 | — | — | — | — | — | **v2 adapter でも停止**: verify window 経路 token 0 で 60 s 無進捗 → exit -6。GPU 側は「released the verify window's GPU waits: the GPU finished in 4 ms」= 完了済み、host の待ちだけが戻らない |
| b1 | `--expert-cache 1`（expert 全 CPU）, prose_1 | 3 | 3/3 | 3.9 / 4.2 / 4.0 | 18762 / 718 | 9 / 4 / 9 | 0 / 0 / 0 | — | rep0 と rep2 は同一本文、rep1 のみ別 |
| b1 | `--expert-cache 1`, tool_1 | 2 | 2/2 | 2.2 / 5.2 | 26412 / 708 | 0 | 0 | Tokyo / Tokyo | prompt 282 tok（llama.cpp 291） |
| b2 | `--kv fp16`, prose_1 | 3 | 3/3 | 4.1 / 4.2 / 4.2 | 19301 / 723 | 21 / 19 / 27 | 1 / 1 / 0 | — | rep1 は簡体字 11 字（们・问・给・过…） |
| b2 | `--kv fp16`, tool_1 | 2 | 2/2 | 2.3 / 4.6 | 25742 / 741 | 0 | 0 | **灯火街** / Tokyo | 同一設定・temp 0 で反復が分かれる |

参考（同一 GGUF、llama.cpp、BOXP-336 l2）: prose_1 は 3 反復とも完全同一本文（423 字、「，」0、簡体字 0、`<think>` 無し）、decode 6.2 tok/s、tool_1 は 3/3 で 灯火街。

#### decode 内訳（STRATA_DECODE_TIMING、engine が出す per-window 集計）

| 実験 | windows | tokens/window | ms/window | GPU-reach wait | per-layer host（CPU expert） | commit/emit | draft | expert cache hit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| a1 rep0 / 1 / 2 | 292 / 608 / 321 | 1.00 / 1.32 / 1.00 | 255 / 280 / 243 | 222 / 245 / 219 | 11.0 / 15.4 / 9.1 | 0.4〜0.7 | 0.00 | 71% / 62% / 74% |
| b1（expert 全 CPU） | 153 / 307 / 153 | 1.00 | 255 / 240 / 248 | 201 / 201 / 201 | 25.4 / 27.2 / 24.0 | 0.2 | 0.00 | 0.3% |
| b2（KV fp16） | 274 / 332 / 338 | 1.00 | 247 / 237 / 238 | 210 / 211 / 211 | 11.7 / 11.0 / 11.4 | 0.7 | 0.00 | — |

#### 解釈

- **decode の遅さの主因は window ごとの GPU 処理（GPU-reach wait 200〜245 ms、1 window の 85〜88%）**。CPU expert 計算は
  VRAM 常駐時 9〜15 ms、全 CPU 時でも 24〜27 ms（4〜11%）で、expert を全部 CPU に移しても ms/window はほぼ変わらない
  （255 → 240〜255）。帯域・PCIe・disk も要因ではない（PCIe 0.3〜2.0 entries/window、state D 0）。
  GPU 側の内訳（attention / dense / router / expert のどれが 200 ms か）は a2 の profile が停止したため未取得。
- **推測 decode は実質無効**: `--spec 4` でも 1.00〜1.32 tokens/window、draft 0 ms。1 token ≒ 1 GPU pass ≒ 240 ms が
  そのまま 4 tok/s になる。iGPU（Arc 140T、engine は "Unknown GPU" 分類で XMX / GDN variant 不使用）でこの 1 pass を短縮する
  手段は engine 側にない（読み出し済み source に iGPU 向け分岐なし）。
- **「，」と簡体字混入は量子化・GPU expert 経路・KV 精度のいずれにも帰属しない**: expert 全 CPU（b1）でも 4〜9 件、
  KV fp16（b2）では 19〜27 件、既定（a1）では 0 件。同じ条件内でも反復で 0〜27 件まで振れる。同一 GGUF の llama.cpp は
  3 反復とも同一本文で 0 件。差は Strata の dense/attention 数値経路または greedy 選択の非決定性にある（未切分け）。
- **temp 0 で出力が非決定的**（a1 の 3 本が全て別本文、b2 tool_1 が 灯火街 / Tokyo に分かれる）。tool_1 の「Tokyo」は
  この非決定性の中で優勢な側で、GPU expert / KV 精度を変えても安定しない。prompt token 282 vs 291 の差（template 本体は
  GGUF 埋め込みと byte 一致）は tokenizer / 描画の段階にあり未切分け。
- a2 は新しい観測: `STRATA_VERIFY_EAGER=1`（verify window を host 待ちで逐次実行）では **v2 adapter でも** host 側の待ちが
  戻らず自己 abort した。GPU は 4 ms で完了していたので、停止は GPU 処理ではなく host の event 待ちの問題という解釈
  （batch3 の結論）と整合する。EAGER は既定 off なので通常経路には影響しない。
- 採用判断は変わらず: 速度（4 vs 6 tok/s）・品質（非決定・簡体字・tool 誤答）とも llama.cpp に劣り、Q2_0 + 今回 backend では
  運用 project を作る根拠がない。高精度 pack（IQ3_XXS/IQ3_S）は未試験（容量目安は vault boxp383-retry.md 追補 3.）。

## 2026-11-03 batch6: 原因識別の有限試験（Owner の深掘り要求、run 20261103T105446Z）

不採用・通常復帰を完了扱いにせず、取得済み Q2_0 / source / ログで原因を識別する。追加 DL・driver 変更・node 再起動はしない。
実 GPU は最大 40 分、識別条件は 3 種まで。stall は条件ごとに打ち切り、同じ停止を盲目的に再試行しない。

- C1: engine 0.1.50 + v2、`STRATA_VERIFY_PROFILE=1`（EAGER なし）+ `STRATA_DECODE_TIMING=1` + `STRATA_FOLLOW`。
  window の段階別 GPU 時間と、同一 token 列での logprob の再現性（同一 process 内 / process 間）。
- C2: engine 0.1.56 + v2、`STRATA_KPROF=1` + `STRATA_VERIFY_EAGER=1`（PROFILE なし）。GPU clock の kernel 単位時間。
- C3: engine 0.1.50 + v2、`STRATA_VERIFY_STEPPED=1` + `STRATA_DECODE_TIMING=1` + `STRATA_FOLLOW`。実行中の host 通信を外した時の速度と再現性。
- 観測（node 側）: GPU 周波数、throttle reason、package power、温度。node の設定は変更しない。

手順は #994 / #996 と同じ: gate `closed:<token>` → inflight 0 を read back → scale 0 → 本 PR merge → 試験 →
strata process 0 を確認 → 復帰 PR → scale 1 → gate `open:<token>` → `/health` と gemma4-26b の実推論を read back。
中止条件: MemAvailable < 12 GiB、node の他 workload 影響。

### batch6 結果（11:14〜11:50 UTC、実 GPU 約 35 分）

- **品質差の主因を特定**: `serve/server.py:8058` の model card 既定 `presence_penalty: 1.5` が temperature 0 の要求にも merge され（`:5084`）、
  直近 64 token（prompt を含む）に出た token の logit が下がっていた。比較した llama.cpp にこの設定はない。logprobs の実測で出力 token ≠ head 1 位が
  14〜16/120 件、`presence_penalty: 0` で 0/298 件。同じ fixtures で「，」0（4/4）、tool 灯火街 3/3、12/12 完走。
- 速度は不変（4.2 tok/s、約 240 ms/window）。C3 stepped は約 10% 速いが品質の修復にはならない。
- 残る未確定: 同一 token 列の logprob が process 間で平均 0.16〜0.30 nat ずれる数値非決定性、簡体字語彙の少数混入、kernel 実行と launch の内訳
  （C2 は計測前に停止）、legacy adapter の停止機構。
- `strata.json` の sampling は変更していない。詳細と訂正は vault `Projects/strata-localllm-evaluation/boxp383-retry.md`。

### batch6 後の source 照合と訂正（run 20261103T115817Z、GPU なし）

保存ログと engine source を照合した結果、本ファイルの過去の記述のうち次を訂正する。詳細は vault `Projects/strata-localllm-evaluation/boxp383-retry.md` の冒頭と batch6 節。

- **停止の機構は未確定**。確定しているのは条件（legacy adapter + batched command list）と回避（`SYCL_UR_USE_LEVEL_ZERO_V2=1` または `UR_L0_USE_IMMEDIATE_COMMANDLISTS=1`）まで。
  「compute / copy queue 間の event 依存が完了しない」は仮説に下げる。copied event への barrier は fused 経路の別分岐で、layer 0 の host grouping 前の wait までに queue 間の明示的な待ちは確認できない。最初の未完了 event は未観測。
- **`GPU-reach wait` の 85〜88% は host の doorbell 待ちの比率**で、GPU 処理の比率ではない。GPU 側が大半というのは推定（flag 待ちを含む区間は 13〜18%、stepped でも改善 5〜10%、expert 全 CPU でも不変）。kernel 別の内訳は C2 が layer 11 で停止して未取得。
- **「finished in 4 ms」は強制解放後の drain 時間**で、GPU 完了の証拠ではない。
- **品質比較は不公平だった**。llama.cpp にも少数の簡体字と `whispered` があり、「英語混入なし」は誤り。tool_1 は token 14 の 灯火 と Tok の差が 0.69〜1.86 で揺れ、penalty 1.5 の閾値をまたぐと結果が割れる。
- 実周波数は要求 2350 MHz に対し平均 1746 MHz で、PL4 reason が稼働サンプルの 90%。

採否は Q2_0 では不採用のままだが、理由は品質ではなく速度（decode 4.1 vs 6.0 tok/s、短文 TTFT 9〜13 vs 3〜5 s）。高精度 pack は未試験。`argoproj/` の manifest は変更しない。
