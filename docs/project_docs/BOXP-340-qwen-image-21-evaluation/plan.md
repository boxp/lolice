# BOXP-340: Qwen-Image-2.1 lolice 実機検証 plan

## 目的

Qwen-Image-2.1 (Turbo / 通常版) を lolice の golyat-4 (Core Ultra 9 285H / Arc 140T iGPU) で実際に動かし、
ひとはこさん / コハコ.exe の全年齢通常イラストを生成して品質・速度・資源を実測する。採用や既定変更は別判断。

## 対象モデル (公式 HF)

- `Qwen/Qwen-Image-2.1-Turbo` revision `fc13ba1986fcb905854dc8de2f52eee395f49b5b` (8-step、CFG=1)
- `Qwen/Qwen-Image-2.1` revision `d26bb61231c349cf6b7896fa83353113880e1ba3` (40-step、品質比較用・必要時のみ)
- license: Qwen RESEARCH LICENSE AGREEMENT (非商用の研究・評価目的のみ)
- 構成: Qwen3-VL 8B text encoder (bf16 17.5GB) + 7B DiT (bf16 14.2GB) + VAE。Turbo 合計約 32.5GB。

## 実行経路

- runtime: 既存 `ghcr.io/boxp/arch/sdnext-ipex` (torch 2.12.0+xpu, Level Zero) を digest 固定で流用し、
  `diffusers==0.42.1` / `transformers==5.21.0` / `accelerate==1.15.0` を `/work/pylib` に入れる。
  driver / kernel / image の変更なし。CUDA 専用コードは使わず device は `xpu`。
- workload: 既存 Argo CD App `stable-diffusion` に `qwen-image-eval` Deployment を追加 (既定 replicas 0)。
  Service なし、ingress なし、egress は DNS と 443 のみ。新 App / AppProject / 新 namespace なし。
- 保存先: golyat-4 `/var/lib/local-llm/models/boxp340-qwen-image-21/` (weights は read-only mount、`work/` に pylib・cache・出力)。
  codex-workspace PVC は使わない。

## GPU 切替 (GitOps)

1. 本 PR: Deployment を replicas 0 で追加 (影響なし)。
2. 切替 PR: `local-llm/llama-server` replicas 0 + `qwen-image-eval` replicas 1。
   merge 前に localllm の進行中要求 / Ready の localllm 票 / 他計測 (BOXP-337 は done) がないことを確認。
3. 復帰 PR: `qwen-image-eval` replicas 0 + `llama-server` replicas 1。merge 後に /health、既定 gemma4-26b、hermes-agent 経路を確認。

## 試験手順

1. gate: RAM / GPU / disk / weights sha256 を取得してから開始。
2. 疎通: 512x512 Turbo 8-step、短い prompt 1 枚。初回 load 時間、peak RAM、GPU を記録。
3. 実用: 1024x1024 (必要なら 1328 前後) で各キャラ複数 seed、文字 prompt のみ。
4. 参照編集: 正本ノートが指定する参照画像を local で入力し identity 保持を比較。
5. 余力があれば通常版 40-step を同 seed 少数で比較。
6. timeout / OOM / 失敗も分母に含める。過負荷 retry はしない。

## 成果物

- 画像: `Projects/qwen-image-21-evaluation/images/` (新規生成物のみ、正本画像は上書きしない)
- `Projects/qwen-image-21-evaluation/owner-summary.md` (日本語)、再現条件ノート
