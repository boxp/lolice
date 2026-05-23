# BOXP-10: Codex workspace Grafana MCP

## Critique

- `~/.codex/config.toml` だけを変更しても、Codex workspace Pod には Grafana service account token が注入されていない。
- `codex-workspace` namespace の Calico NetworkPolicy は egress を制限しており、Grafana の `monitoring` namespace への TCP 3000 を明示許可する必要がある。
- Grafana 側の Kubernetes NetworkPolicy も ingress を制限しており、`codex-workspace` Pod からの TCP 3000 を明示許可する必要がある。
- OpenClaw は撤去済みなので、OpenClaw 由来の Secret 名や SSM path は再利用しない。

## Implementation

- `argoproj/codex-workspace/external-secret.yaml`
  - `ExternalSecret/codex-workspace-grafana` を追加する。
  - SSM Parameter Store の `/lolice/codex-workspace/grafana-service-account-token` を `Secret/codex-workspace-grafana` に同期する。
- `argoproj/codex-workspace/deployment.yaml`
  - `GRAFANA_URL=http://grafana.monitoring.svc.cluster.local:3000` を注入する。
  - `GRAFANA_SERVICE_ACCOUNT_TOKEN` を `Secret/codex-workspace-grafana` から注入する。
- `argoproj/codex-workspace/networkpolicy.yaml`
  - Codex workspace から Grafana Pod への TCP 3000 egress を許可する。
- `argoproj/prometheus-operator/overlays/network-policy-grafana.yaml`
  - Grafana Pod への Codex workspace からの TCP 3000 ingress を許可する。
- `~/.codex/config.toml`
  - dotfiles ではなく、この workspace の home 配下だけで Grafana MCP server を登録する。

## Verification

- `kubectl kustomize argoproj/codex-workspace`
- `kubectl kustomize argoproj/prometheus-operator`
- `codex mcp list`

## Manual prerequisite

- Grafana service account token を SSM Parameter Store の `/lolice/codex-workspace/grafana-service-account-token` に SecureString として登録する。
