# BOXP-250: local-llm Envoy の stream idle timeout を有限値に設定する

## 背景

- Novel Board runner（codex-workspace sidecar）が `assignee::localllm` の NOVEL-6 groom で 3 回とも約 300 秒ちょうどで `HTTP 504 stream timeout` を受けて失敗した（run `20261019T090234Z-b0814a8f…` の `llm-evidence.edn`: elapsed 300,016 / 300,013 / 300,018 ms）。
- runner 側の HTTP client 上限は 1800 秒であり、client の待機不足ではない。llama-server（cpp-httplib）は 504 を返さない。
- `argoproj/local-llm/envoy-config.yaml` は route の `timeout` と `max_stream_duration` を `0s`（無制限）にしているが、HTTP connection manager の `stream_idle_timeout` は未設定で Envoy 既定の 5 分が有効。非 streaming の chat completion は応答本文が完成するまで 1 byte も流れないため、prefill + 生成が 300 秒を超える要求は Envoy が `504 stream timeout` で打ち切る。
- 実測（Service 経由、gemma4-26b）: prefill 86.6 token/s（8,281 token で 95.6 秒）。NOVEL-6 の groom prompt は 15,807 token で prefill だけで約 180〜220 秒、出力 1,500〜2,500 token（約 11 token/s）を加えると 300 秒を超える。NOVEL-4 の write（6,392 token, 241.9 秒）が通ったのは 300 秒未満に収まったため。

## 方針

- `stream_idle_timeout: 1800s` を HCM に設定する。無制限（`0s`）にはしない。値は Novel Board runner の 1 要求あたり total 上限（30 分）に合わせる。
- 併せて arch 側で runner を SSE streaming に変更し（boxp/arch PR）、通常は token が流れ続けるので Envoy の idle 判定にはかからない。本変更は prefill / 待ち行列中に 300 秒以上無通信になる場合と、非 streaming の他クライアントのための防御。

## 反映

- ConfigMap `llama-server-envoy` の更新のみ。Deployment は `configmap.reloader.stakater.com/reload: "llama-server-models"` で models ConfigMap だけを監視しているため、本 PR の merge / Argo CD sync で llama-server Pod は再起動しない（推論中断なし）。
- Envoy は静的設定のため、live への反映は次回 llama-server Pod 再起動時（image 更新・ノード保守など）。即時反映が必要なら owner 判断で Pod を再作成する（モデル再ロードで数分の提供停止を伴う）。

## 検証

- kustomize build が通ること（CI）。
- 反映後: 非 streaming で 300 秒超の要求が 504 にならないこと（Novel Board runner の evidence または Envoy stats `downstream_rq_idle_timeout` で確認）。
