#!/usr/bin/env bash
# BOXP-462: renders the Alertmanager -> Hermes relay fixture from the production files.
# usage: render-fixture.sh <token>   (prints a manifest stream on stdout)
set -euo pipefail
: "${REPO_ROOT:?REPO_ROOT is required}"
token="$1"
ns=alertmanager-relay-fixture
here="$REPO_ROOT/tests/chainsaw/hermes-agent/alertmanager-relay"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
yq -r '.spec.target.template.data["alertmanager.yaml"]' "$REPO_ROOT/argoproj/prometheus-operator/external-secret-alertmanager.yaml" > "$tmp/am.tmpl"
# The token must come from a mounted file: prometheus-operator re-marshals the config
# and replaces http_headers `secrets:` values with "<secret>" (seen in production).
grep -q "files: \['/etc/alertmanager/secrets/alertmanager-hermes-webhook-token/token'\]" "$tmp/am.tmpl" || { echo 'production Alertmanager template does not read the Hermes token file' >&2; exit 1; }
if yq -e '[.. | select(tag == "!!map" and has("http_headers")) | .http_headers[] | select(has("secrets"))] | length > 0' "$tmp/am.tmpl" >/dev/null 2>&1; then
  echo 'http_headers.secrets is masked by prometheus-operator; use files' >&2; exit 1
fi
grep -q 'hermes-agent-webhook.hermes-agent.svc.cluster.local:8644/webhooks/alertmanager' "$tmp/am.tmpl" || { echo 'production Hermes URL changed' >&2; exit 1; }
# Substitute the ESO template values and keep mail inside the fixture (no external SMTP).
sed -e 's|{{ .resend_api_key }}|fixture|' \
    -e 's|{{ .alert_email_to }}|nobody@example.invalid|' \
    -e 's|{{ .alert_email_from }}|fixture@example.invalid|' \
    -e 's|smtp.resend.com:587|127.0.0.1:25|' \
    -e "s|hermes-agent-webhook.hermes-agent.svc.cluster.local|hermes-sink.$ns.svc.cluster.local|" \
    "$tmp/am.tmpl" > "$tmp/alertmanager.yaml"
if grep -q '{{' "$tmp/alertmanager.yaml"; then echo 'unsubstituted template value' >&2; exit 1; fi
yq -r '.data["alertmanager_relay.clj"]' "$REPO_ROOT/argoproj/hermes-agent/configmap.yaml" > "$tmp/relay.clj"
test -s "$tmp/relay.clj"
kubectl create namespace "$ns" --dry-run=client -o yaml
echo ---
kubectl create secret generic alertmanager-config -n "$ns" --from-file=alertmanager.yaml="$tmp/alertmanager.yaml" --dry-run=client -o yaml
echo ---
kubectl create secret generic fixture-token -n "$ns" --from-literal=token="$token" --dry-run=client -o yaml
echo ---
kubectl create secret generic alertmanager-hermes-webhook-token -n "$ns" --from-literal=token="$token" --dry-run=client -o yaml
echo ---
kubectl create configmap fixture-scripts -n "$ns" --from-file=sink.py="$here/sink.py" --from-file=relay.clj="$tmp/relay.clj" --dry-run=client -o yaml
echo ---
cat "$here/fixture.yaml"
