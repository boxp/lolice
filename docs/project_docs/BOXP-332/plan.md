# BOXP-332: Kohako G2 の実 Hermes 会話（lolice 側）

全体計画は boxp/even-g2-lab `docs/project_docs/BOXP-332/plan.md`。lolice では Hermes の公式 API server（v0.22.5 Runs API）を Kohako G2 BFF だけに開く。

- hermes-agent：`API_SERVER_ENABLED=true`、`0.0.0.0:8642`、`API_SERVER_KEY` は ESO Password generator でクラスタ内生成（BOXP-270 と同じ方式。外部ストア・手入力・新 provider なし）。
- toolset：`platform_toolsets.api_server: [no_mcp]`（tool なし・MCP なし。`[]` だけだとグローバル MCP が足される）。PVC の config.yaml には bootstrap-config で 1 行だけ挿入（既存の値は広げない。違う値なら警告して書き換えず、BFF は `/v1/toolsets` を見て送信を拒否する）。
- 到達：Service `hermes-agent-api`（ClusterIP）、Calico ingress は even-g2-lab の `app == 'even-g2-main'` から 8642 だけ。even-g2-main の egress に hermes-agent:8642 を追加。
- 鍵の受け渡し：even-g2-lab の ESO Kubernetes-provider SecretStore（Role で 1 Secret の get だけ）→ Secret `kohako-hermes-api-key` → kohako-bff sidecar の env だけ（web container・端末には渡さない）。
- 確認：Hermes Pod 内から `/health`・`/v1/toolsets` が全 disabled・検証用 session で `/v1/runs` 1 往復。Telegram の動作継続。

rollback：本 PR の revert（`API_SERVER_ENABLED=false` に戻り、BFF は鍵が無くなるので Pod 再作成時に Secret 参照で止まる → 先に even-g2-lab の deployment env を戻す順）。

## 追補（2026-10-27 音声入力 M5：lolice 内 whisper.cpp）

Owner 承認済み：G2 マイク → BFF → lolice 内 whisper.cpp（CPU のみ）→ 認識文を G2 で確認 → 送信。外部 STT・課金・新 credential なし。

- `deployment-kohako-stt.yaml`：`even-g2-lab/kohako-stt`。公式 image `ghcr.io/ggml-org/whisper.cpp:main` を digest 固定。init container が `ggml-small-q5_1.bin` と silero VAD を Hugging Face の固定 revision から取得し sha256 を検証（失敗なら起動しない）。`whisper-server --language ja --threads 2 --suppress-nst --vad --no-gpu`。golyat-4 に固定（GPU は使わない）。requests CPU 2 / メモリ 1Gi、limits CPU 2 / メモリ 2Gi。非 root・read-only root・SA token なし。ClusterIP Service `kohako-stt:8080`。
- `networkpolicy-kohako-stt.yaml`：ingress は `even-g2-main` から 8080 だけ。egress は DNS と公開 IP の 443（init container の model 取得用。Calico は container 単位で絞れない。whisper-server 自体は外へ通信しない）。
- `even-g2-main`：egress に `kohako-stt:8080` を追加し、BFF に `KOHAKO_STT_URL`。BFF が対応していない旧 image では env は無視される。
- 実測（同じ image・引数・CPU 2・メモリ 2GiB、合成日本語）：1 件 3.6〜4.7 秒、RSS 約 0.35〜0.42GiB。無音・雑音は VAD で空。ログに認識本文は出ない。

rollback：本 PR の revert（BFF は `KOHAKO_STT_URL` が無くなり 503 `stt_disabled`、G2 は「音声: STT未接続」でマイクを開かない）。
