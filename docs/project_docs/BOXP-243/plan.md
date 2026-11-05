# BOXP-243 local-llm Gemma4 実機ベンチ最適化

## 目的

lolice の `local-llm`（golyat-4 / Core Ultra 9 285H + Arc 140T iGPU、llama-server SYCL build b9840-8c146a836）で稼働する
`gemma4-26b`（Gemma4-26B-A4B Q4_K_M、ctx 262144、f16 KV、`-fa auto`）について、同一モデル・同一量子化・同一 context・
同等出力品質を保ったまま single stream の decode token/s を最大化する構成を実機で探索し、最良の安定候補だけを IaC に反映する。

## 現状の同定（2026-10-18 21:40 JST）

- image: `ghcr.io/boxp/arch/llama-sycl:latest@sha256:8f864379…`（Argo source override）、llama.cpp rev `8c146a83`（b9840）。image に `llama-bench` は無い（`llama-server` target のみビルド）。
- 起動: router mode（`--models-preset /config/models.ini --models-max 1`）。gemma4-26b 子プロセス引数は `-ngl 99 -c 262144 -b 2048 -ub 1024 -t 14 -tb 14 -fa auto --no-mmap --jinja --reasoning off --no-warmup --parallel 1`。
- node: 16 論理 CPU（6P+8E+2LPE）、RAM 96GiB、swap なし、Ubuntu 24.04 / 6.8.0-146、libze 1.28.2 / compute-runtime 26.18。同一ノードに ark-server（CPU 1–3 core 常用）が同居。
- 利用者: codex-workspace の pi config（gemma4-26b / gemma4-26b-vision / ornith-35b）、hermes-agent（ornith-35b）。Envoy 累計 18k req のほぼ全てが health check で実推論は低頻度。

## 測定方法

- cluster の RBAC では exec/Job 作成不可のため、既存 SSH 鍵 + `sudo crictl exec` で稼働中 llama-server コンテナ内に候補 `llama-server` を一時起動（別ポート、同 image/同 lib/同 GPU）し、コンテナ内 `curl` で `/completion` を叩いて server 報告の `timings` を記録する。
- 事前に router の `gemma4-26b` を idle 確認のうえ unload し、GPU/メモリの二重ロードを避ける。候補は逐次 1 本ずつ起動・停止する。
- 固定 prompt（無害な英日テキスト、約 512 token）、`n_predict=128`、`ignore_eos=true`、`temperature=0`、`seed=42`、`cache_prompt=false`。warmup 1 回を捨て、3 反復の中央値と raw 値を保存。prefill は pp512 と pp2048 を別指標として記録。
- 品質確認: 日本語一文・英語の簡単な既知解を `/v1/chat/completions` で確認し、文字化け/無限反復/空出力/segfault を棄却条件とする。
- 探索順: 一変数比較（threads、-fa、KV 型、MoE offload、batch/ubatch、poll/prio、SYCL 実行環境変数、ngram speculative）→ 上位の組合せ → 再測定。総占有時間は 6 時間上限、02:00–09:00 JST は新規試行なし。

## 反映

1. 最良の安定候補を `argoproj/local-llm/models-config.yaml` / `deployment.yaml` の最小差分で反映し PR。
2. CI / codex-review / merge / Argo 同期 / live args 確認後、service endpoint（`llama-server.local-llm.svc:8080`）経由で同一条件の before/after を実測。小差（ノイズ範囲）なら既存へ戻す。
3. 正本: Obsidian `Projects/local-llm-gemma4-optimization/README.md` と `benchmark-results.md`、raw log は同 `artifacts/BOXP-243/`。

## 制約と復旧

- host/kernel/driver/BIOS/cluster 変更なし、新 backend 常設なし、llama-vulkan 復活なし、SD.Next/cloudflared 停止維持、Secret 探索なし。
- CVE-2026-99999 修正版が公開（BOXP-242 監視の handoff）されたら新規 bench を止め元状態へ復帰する。
- 終了時は一時プロセスを全て停止し、router に `gemma4-26b` を再ロードして health と短い推論を確認する。
