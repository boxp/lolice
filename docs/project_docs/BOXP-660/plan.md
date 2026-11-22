# BOXP-660: Hy4-flash 量子化版（IQ2_AH）の有限実機試験

チケット: BOXP-660。正本: Obsidian `Projects/hy4-flash-evaluation/README.md`。型は BOXP-557（lolice#1247 → #1249）と同じ。

## 目的

`AngelSlim/Hy4-flash-GGUF` の IQ2_AH を golyat-4（Arc 140T iGPU、RAM 93.8 GiB）で実際にロード・推論し、日本語品質・速度・初回待ち・メモリ・SSD 読込・継続安定性を測って現行 gemma4-26b と比べる。恒久採用、preset 追加、既定の切替、Hermes / TaskRunner の変更は含まない。SPARK-4Bit / Fast は NVIDIA Spark（CUDA）専用で実機がないため対象外（未検証と明記する）。

## artifact

- GGUF: rev `bcbc7db5dcf4162cc1e1b8ed6c65567e843ebe46`、Tencent Hy License 1.0（内部評価は対象外条項 10.3 の範囲）
  - shard 1（本体）71,635,656,768 B、sha256 `e7f61037aa079f9ebac66f84b905fd040a2cd06b2e47d07809542f5bb29dda1e`
  - shard 2（Engram 表、lazy mmap）63,360,616,128 B、sha256 `ecd6a0ecd7f258b1ee9211bf566261ae33efc1743ad40f58cb5004cf7b051839`
  - 置き場所: node の `/var/lib/local-llm/models/boxp660-hy4-flash/`（`dl.sh`、`dl.log`、`SHA256SUMS` も同じ場所）
- 評価 image: `ghcr.io/boxp/arch/llama-sycl@sha256:9815bd18fa8a385dae2f110569074a9283e5841c867dbf3a0f180a38cd02e371`（boxp/arch#15479 の `pr-15479`。llama.cpp PR ggml-org/llama.cpp#32517 head `fc0b8338a9ba02c04499e9bd291821648c97f7d3`。#15479 は merge せず、`latest` は動かさない）
- PR #32517 の差分は監査済み（`ggml/`・build script・server・CI は無変更、外部通信 / process 起動 / file 書込の追加なし）。AngelSlim の CUDA 用 patch と trust-remote-code は使わない。

## このリポジトリで行う変更（試験の間だけ）

- `argoproj/local-llm/deployment.yaml`: replicas 0（Argo は `/spec/replicas` を ignore。実 scale は runtime file queue）
- `argoproj/strata-eval/manifests/deployment.yaml`: replicas 1、評価 image、`sleep infinity`、memory limit 80GiB、`/s` = `/var/lib/local-llm/models/boxp660-hy4-flash`（読み取り専用）、`/w` = `/var/lib/local-llm/models/boxp660`（測定記録と試験スクリプト）。Service なし、token mount なし、秘密なし（既存のまま）。

試験の終了時（成功・失敗・長期中断のどれでも）に別の復帰 PR でこの差分を戻す。`models-config.yaml` は変更しない。

## 手順

0. merge 前の確認: 2 shard の sha256 照合が済んでいること、node の `/var/lib/local-llm/models/boxp660`（boxp:boxp 775、作成済み）と `boxp660-hy4-flash` が存在すること、board 上に GPU を取り合う進行中の票がないこと。
1. 受付 close（`closed:BOXP-660-<時刻>`）→ 全 Pod で読み戻し → 外部要求が 503 → upstream active / pending 0 → runtime file queue で llama-server を scale 0 → GPU とメモリの解放を確認 → この PR を merge。動作中の GPU 作業は強制停止しない。
2. node から `crictl exec` で llama-server を起動する（`node660.sh`）。段 A: CPU のみ（`-ngl 0`、mmap、ctx 8192）→ 段 B: 部分 offload（層数を段階的に。全層に固執しない）→ 段 C: 557 と同じ 15 要求（灯火街 fixture）→ 段 D: 20〜30 分の連続要求。上の段が通った時だけ次へ進む。
3. 上限: 1 段あたり llama 枠 15〜30 分、全体 45 分、ready の上限 600 秒。
4. 中止条件: MemAvailable < 12GiB が 2 連続、GPU HANG / reset の増加、ready せず。設定を変えた盲 retry はしない。
5. 復帰: 試験 process 停止 → 復帰 PR → strata pod 消滅 → scale 1 → `/health` → 受付を閉じたまま gemma4-26b の実推論 → 受付 `open:BOXP-660-<時刻>-end` → 外部経路で 1 件確認。

## 境界

既存 model / 唯一 artifact の削除、driver / kernel の変更、node reboot、権限追加、恒久的な既定変更、モデルが出した tool call の実行はしない。入力は合成 fixture だけで、私的本文・秘密は使わない。
