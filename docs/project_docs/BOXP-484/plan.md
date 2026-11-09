# BOXP-484 通常SD・llamaの動的切替

## 目的
通常のSD入口からの画像生成要求で、既存LLMのdrain → GPU解放 → SD.Next起動 → 生成 → LLM復帰を、毎jobのGit/Argo操作なしで実行する。

## 構成
- `sd-admission-gateway`（常設、GPUなし、K8s権限なし）: 既存Service `stable-diffusion-webui` の背後に置く。Tunnel設定とAccess認証は変更しない。
- `sd_intake.py`: 生成POSTを永続受付して202とjob IDを返す。結果不明のjobは再送しない。
- `runtime_switch_worker.py`: 既存controller Pod内で動く。tokenを持たず既存の条件付きファイルtransportのみを使う。gateのCASが他のGPU所有者との排他になる。
- K8s AdmissionWebhookは使わない（drainやGPU解放ができないため）。

## 状態遷移
llm-open → fencing → draining → stopping-llm → starting-sd → image-ready → image-draining → stopping-sd → starting-llm → verifying → reopening → llm-open。異常時は `failed` で受付を閉じたまま停止する。

## 公平性と上限
- 画像windowは最大30分で、実行中の生成は止めない。アイドル5分で解放する。
- window間でLLMを最低120秒開ける。drainが35分を超えたらllamaに触らず受付を戻す。
- SDの起動失敗時はLLMへ戻し、1時間は再試行しない。

## Owner承認が必要な差分（`[owner-approval-required]` コミット）
1. 既存SAへの `deployments/scale` `stable-diffusion-webui` get/patch
2. Argoの `stable-diffusion-webui` `/spec/replicas` を runtime 所有へ移管
3. `stable-diffusion-cloudflared` を1台で起動（既存のAccess保護URLを再開）

## 検証
- 隔離: `tests/sd-switch`（状態遷移、再起動、結果不明、競合、実Envoy経路）と `tests/chainsaw/stable-diffusion`
- 実機: 承認・配備後に通常入口から小さい画像1枚 → LLM推論 → 受付復帰を確認する

## 既知の制約
- 生成APIは同期ではなく202とポーリングになる（Cloudflareの応答時間上限のため）。
- WebUI操作が5分ないとwindowを解放する。
- 実機のSD.Nextで `/sdapi/v1/progress` の形式を確認するまで、アイドル判定は未検証。

## 独立レビューの反映（head 16e76f7 時点）
- 修正: slots の 503 を idle 扱いしない / image-ready 中の `/sdapi/` 書込みは生成の受付以外を SD.Next へ通さない / gateway の LAN 許可を削除し Tunnel のみに限定 / dispatch と image-draining の競合を再確認で解消。
- 追加修正: SD.Next は `end()` 後も `sampling_step` を残し `progress` が 0 に戻らないため、idle 判定は `job` と `job_count` のみで行う。
- 非採用（P2）: Deployment の `status.replicas` に Terminating Pod が含まれない件。`gpu.intel.com/i915` allocatable=1 のためスケジューラが後続 GPU Pod を Pending にし、二重所有は起きない。
- 残る制約: WebUI の Gradio 経由の操作（再起動ボタン等）は gateway で個別に制限していない。Access 認証済みの利用者のみ到達できる。
