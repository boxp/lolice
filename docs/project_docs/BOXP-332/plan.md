# BOXP-332: Kohako G2 の実 Hermes 会話（lolice 側）

全体計画は boxp/even-g2-lab `docs/project_docs/BOXP-332/plan.md`。lolice では Hermes の公式 API server（v0.22.5 Runs API）を Kohako G2 BFF だけに開く。

- hermes-agent：`API_SERVER_ENABLED=true`、`0.0.0.0:8642`、`API_SERVER_KEY` は ESO Password generator でクラスタ内生成（BOXP-270 と同じ方式。外部ストア・手入力・新 provider なし）。
- toolset：`platform_toolsets.api_server: []`（tool なし）。PVC の config.yaml には bootstrap-config で 1 行だけ挿入（既存の値は広げない。違う値なら警告して書き換えず、BFF は `/v1/toolsets` を見て送信を拒否する）。
- 到達：Service `hermes-agent-api`（ClusterIP）、Calico ingress は even-g2-lab の `app == 'even-g2-main'` から 8642 だけ。even-g2-main の egress に hermes-agent:8642 を追加。
- 鍵の受け渡し：even-g2-lab の ESO Kubernetes-provider SecretStore（Role で 1 Secret の get だけ）→ Secret `kohako-hermes-api-key` → kohako-bff sidecar の env だけ（web container・端末には渡さない）。
- 確認：Hermes Pod 内から `/health`・`/v1/toolsets` が全 disabled・検証用 session で `/v1/runs` 1 往復。Telegram の動作継続。

rollback：本 PR の revert（`API_SERVER_ENABLED=false` に戻り、BFF は鍵が無くなるので Pod 再作成時に Secret 参照で止まる → 先に even-g2-lab の deployment env を戻す順）。
