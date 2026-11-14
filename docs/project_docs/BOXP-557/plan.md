# BOXP-557: Qwen3.8-Flash-Next GSQ-RCO IQ3_S の有限実機試験

チケット: BOXP-557。親資料: Obsidian `Projects/strata-localllm-evaluation/boxp383-retry.md`（batch7 の IQ3_XXS 比較）。

## 目的

`ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF` の IQ3_S を golyat-4 で実際にロード・推論し、速度・初回待ち・資源・正否を記録する。恒久採用、既定の切替、preset 追加は含まない。

## artifact

- rev `ed59f92082b1e93c0e96d60a8b11aab089b52f09`（main、2026-09-29 から変化なし）、Apache-2.0
- shard 1: `IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf`、54,817,524,224 B、sha256 `4c1eb2ce…aca3`
- shard 2（PLE 表）: Q2_0 shard 2 と同じ sha256 `316b46f3…` なので hardlink で再利用（新規取得なし）
- 置き場所: node の `/var/lib/local-llm/models/boxp336-strata/flash-next-iq3_s/`（既存 model store。workspace PVC と vault には置かない）

## このリポジトリで行う変更（試験の間だけ）

- `argoproj/local-llm/deployment.yaml`: replicas 0（Argo は replicas を ignore。実 scale は runtime file queue）
- `argoproj/strata-eval/manifests/deployment.yaml`: replicas 1、command `sleep infinity`、memory limit 76GiB、`/w` = `/var/lib/local-llm/models/boxp557`（測定記録と試験スクリプト）の書き込み mount

試験の終了時に別の復帰 PR でこの差分を戻す。

## 手順

0. node 上に `/var/lib/local-llm/models/boxp557` を boxp（uid/gid 1000）所有・775 で作成する（`hostPath.type: Directory` は自動作成しない。2026-11-14 21:40 UTC に作成済みで `stat` で `boxp:boxp 775` を確認）。merge 前に再度 `stat` で存在を確認し、無ければ merge しない。
1. 受付 close（`closed:BOXP-557-<run>`）→ upstream active/pending 0 → scale 0 → GPU とメモリの解放を確認 → この PR を merge。
2. backend は live と同じ image（llama.cpp b11669）。BOXP-383 batch7 の llama.cpp 側と同じ flag・fixture・bench（15 要求：日本語 prose、code、8k 長文、JSON、tool）。
3. 上限: llama 枠 30 分、GPU 全体 45 分。ready の上限 600 秒。
4. 中止条件: MemAvailable < 12GiB が 2 連続、GPU HANG/reset の増加、ready せず。失敗しても同じ操作を盲目的に再試行しない。
5. 終了: 復帰 PR → strata pod 消滅 → scale 1 → 受付を閉じたまま Gemma 実推論 → 受付 open → 外部経路で確認。

## 境界

既存 model の削除、driver / kernel の変更、node reboot、権限追加、恒久的な既定変更はしない。sd-switch（画像/動画の GPU 切替）は gate の compare-and-set で排他され、llama=1・gate open の時しか動かない。
