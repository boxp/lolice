# BOXP-365: Hermes へ Grafana 読取認証を供給し MCP 実照会を確認する

## 目的

Codex と同様に Kohako / Hermes から Grafana MCP を使えるようにする。既存の read-only Grafana 認証経路（ExternalSecret 方式）を再利用し、Hermes 自身の MCP 呼出しで `ai_ops_hermes_events_total` と `up{job=~".*ai-ops.*"}` を実照会する。

## 調査結果

- codex-workspace は SSM `/lolice/codex-workspace/grafana-service-account-token` を ExternalSecret で取り込み、`grafana/mcp-grafana --disable-write` (stdio) を使っている。
- この token の service account (`sa-1-mcp-grafana`) は、権限一覧がすべて `:read` / `:query` / list だけ（Viewer 相当、`isGrafanaAdmin=false`）。必要な read-only 範囲を超えないため、そのまま再利用する。値は表示していない。
- Hermes v0.22.5 公式 catalog の `grafana` は Grafana Cloud hosted（OAuth・Assistant 課金あり）用なので、self-hosted の lolice には使えない。OSS の `mcp-grafana` を手動で構成する方法が公式に案内されている。
- Hermes の stdio MCP は、子プロセスへ渡す env を safe key に絞る。token を Hermes コンテナの env に置くと、terminal tool の届く範囲に token が入る。

## 方針

1. `hermes-agent` namespace に ExternalSecret `external-secret-hermes-agent-grafana` を追加する（同じ SSM parameter → Secret `hermes-agent-grafana`）。
2. Pod に sidecar `grafana-mcp`（`grafana/mcp-grafana:2.1.1`、digest 固定）を追加する。
   - `127.0.0.1:8000` の streamable-http（loopback のみ。Service / ports は作らない）
   - `--disable-write`、`--usage-stats=disabled`（Grafana Labs への匿名統計送信を止める）、`--enabled-tools` は search / datasource / prometheus / loki / dashboard / folder / alerting / annotations に限定する（汎用 `api` tool・rendering・外部 docs は含めない）
   - token は sidecar の env にだけ secretKeyRef で注入する。hermes-agent コンテナには渡さない。
   - nonroot、readOnlyRootFilesystem、capabilities drop ALL
3. NetworkPolicy:
   - Calico `hermes-agent-network-policy` に egress（monitoring/grafana:3000）を追加する
   - `monitoring/grafana` の ingress に hermes-agent Pod を追加する
4. rollout 後、公式 interface `hermes mcp add grafana --url http://127.0.0.1:8000/mcp`（認証なし・loopback）で登録する。`mcp_servers` の手編集はしない。
5. `hermes chat` の oneshot で Hermes 自身に datasource UID の発見と 2 query を行わせ、取得時刻 / label / 件数 / unknown / 欠損を区別して記録する。

## 安全境界

- Secret 値を LLM / chat / log / git に出さない。agent に Kubernetes secret get 権限を追加しない。
- api_server (G2) の `no_mcp` と kohako-g2-context の decision guard はそのまま維持する。
- 新しい公開経路・課金・provider 変更・外部送信は追加しない。
- 反映は通常の GitOps（PR merge → Argo CD）で行う。Hermes Pod の再作成は 1 回を見込む。
