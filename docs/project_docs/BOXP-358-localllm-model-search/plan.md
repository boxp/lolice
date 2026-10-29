# BOXP-358: lolice 実機で有力ローカル LLM を継続比較する（計画）

正本: Obsidian `Projects/localllm-model-search/owner-summary.md`（結果・ランキング・失敗理由はそちらに記録する）。

## 開始時の live 条件（2026-10-29 01:4x UTC 実測）

- golyat-4: Core Ultra 9 285H（16 thread、AVX-512 なし）、Arc 140T iGPU（i915、VRAM は共有 RAM）、RAM 93 GB、swap なし、MemAvailable 約 60 GB（gemma4-26b loaded 時）。
- NVMe `/` 1.9 TB、空き 999 GB。
- llama.cpp b11669 SYCL（image `llama-sycl@sha256:f74b5341…`）、router `--models-max 1 -c 262144 --parallel 1 -fa auto --reasoning off --jinja`。
- 既定 `gemma4-26b`（loaded）、admission state open、直近 24h に推論要求なし、qwen-image-eval replicas 0。

## 資源上限（開始時に固定）

| 項目 | 上限 |
| --- | --- |
| node MemAvailable 下限 | 12 GiB を下回ったら候補を router の通常 unload で解放（watchdog） |
| weights + KV + buffer | 候補 1 つで 55 GB 以下（推定と実測を両方記録） |
| model store 空き reserve | 800 GB 以上を常に残す |
| 本票の download 保持上限 | 合計 150 GB（`/var/lib/local-llm/models/boxp358/`） |
| 1 候補の時間上限 | 取得を除き 90 分（smoke 失敗時の調整は 2 回まで） |

## 方式

- 候補は通常 PR で models.ini に **選べる preset** として追加する（既定・Deployment 引数は変えない）。`--models-max 1` のため GPU は一度に 1 候補。
- 比較 harness は BOXP-337 と同じ（F: 決定的 9 種 × 3、L32: 32k needle、B253: pp/tg + 日本語回帰、A1: 合成 fixture 灯火街、A2: 実バース本文は Owner vault のみ）。
- 各 batch 後に gemma4-26b を再ロードし、health / 短文推論 / 受付 open / GPU 解放を確認する。
