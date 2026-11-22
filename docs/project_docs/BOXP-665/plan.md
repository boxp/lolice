# BOXP-665: Inkling 2 Mini の有限実機試験（Gemma 4 との比較）

チケット: BOXP-665。正本: Obsidian `Projects/inkling-2-mini-evaluation/README.md`。型は BOXP-660（lolice#1349 → #1355）と同じ。

## 目的

Thinking Machines Lab の Inkling 2 Mini（Apache-2.0、26.4B MoE、loop 型）の GGUF を golyat-4（Arc 140T iGPU、RAM 93.8 GiB）で実際にロード・推論し、日本語の会話・設定参照・創作、速度、メモリ、継続安定性を測って現行 gemma4-26b と比べる。恒久採用、preset 追加、既定の切替、Hermes / TaskRunner の変更は含まない。DGX Spark の実機はないため原 checkpoint（NVFP4 / MXFP8）は対象外（未検証と明記する）。

## artifact

- GGUF: `ggml-org/Inkling-2-Mini-GGUF` rev `e8a2cecf0e78914f07cc27b9be711a9b2c7561d9`
  - `Inkling-2-Mini-IQ4_NL.gguf` 17,627,940,768 B、sha256 `cbdf3fa9dc50a4124977980f37da4f1fa764b0cfe800d275cd43d26ce2a81af4`（主。照合済み）
  - `Inkling-2-Mini-Q8_0.gguf` 27.8 GB、sha256 `d57542f8aea5d7639beba5fc4ae7f584754916f63ac5d5a69328c27f652f45da`（副。取得と照合が済んだ時だけ使う）
  - 置き場所: node の `/var/lib/local-llm/models/boxp665-inkling2-mini/`
- 評価 image: `ghcr.io/boxp/arch/llama-sycl@sha256:e9505a17ca5f5073f3c007fe8a9f8653664e1aeb35cf0d1858b1ad27738f37d6`（boxp/arch#15534 の `pr-15534`。llama.cpp の release `b12633` = `9c10c94e`、Inkling 対応の ggml-org/llama.cpp#32694 と loop 修正 #32704 を含む upstream master 上の commit。#15534 は merge せず、`latest` は動かさない）

## このリポジトリで行う変更（試験の間だけ）

- `argoproj/local-llm/deployment.yaml`: replicas 0（Argo は `/spec/replicas` を ignore。実 scale は runtime file queue）
- `argoproj/strata-eval/manifests/deployment.yaml`: replicas 1、評価 image、`sleep infinity`、`/s` = `/var/lib/local-llm/models/boxp665-inkling2-mini`（読み取り専用）、`/w` = `/var/lib/local-llm/models/boxp665`（測定記録と試験スクリプト）。memory limit は 56GiB のまま。Service なし、token mount なし、秘密なし（既存のまま）。

試験の終了時（成功・失敗・長期中断のどれでも）に別の復帰 PR でこの差分を戻す。`models-config.yaml` は変更しない。

## 手順

0. merge 前の確認: GGUF の sha256 照合、node の directory、board 上に GPU を取り合う進行中の票がないこと。
1. 受付 close（`closed:BOXP-665-<時刻>`）→ 外部要求が 503 → upstream active / pending 0 → runtime file queue で llama-server を scale 0 → GPU とメモリの解放を確認 → この PR を merge。動作中の GPU 作業は強制停止しない。
2. node から `crictl exec` で llama-server を起動する（`node665.sh`）。段 A: CPU のみ → 段 B: 全層 offload（SYCL）→ 速い方で段 C: BOXP-660 の gemma 比較と同じ 15 要求（灯火街 fixture、reasoning_effort none）→ 段 D: 連続要求 → 余力があれば reasoning 既定値・temperature 違い・Q8_0 を少数回。
3. 上限: 1 段あたり llama 枠 15〜30 分、ready の上限 600 秒。
4. 中止条件: MemAvailable < 12GiB が 2 連続、GPU HANG / reset の増加、ready せず。設定を変えた盲 retry はしない。
5. 復帰: 試験 process 停止 → 復帰 PR → strata pod 消滅 → scale 1 → 受付を閉じたまま gemma4-26b の実推論 → 受付 `open:BOXP-665-<時刻>-end` → LAN の通常経路で 1 件確認。

## 境界

既存 model / 唯一 artifact の削除、driver / kernel の変更、node reboot、権限追加、恒久的な既定変更、モデルが出した tool call の実行はしない。入力は合成 fixture だけで、私的本文・秘密は使わない。
