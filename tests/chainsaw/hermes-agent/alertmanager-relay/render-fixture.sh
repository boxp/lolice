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
grep -q "{{ .hermes_webhook_token }}" "$tmp/am.tmpl" || { echo 'production Alertmanager template has no Hermes token' >&2; exit 1; }
grep -q 'hermes-agent-webhook.hermes-agent.svc.cluster.local:8644/webhooks/alertmanager' "$tmp/am.tmpl" || { echo 'production Hermes URL changed' >&2; exit 1; }
# Substitute the ESO template values and keep mail inside the fixture (no external SMTP).
sed -e "s|{{ .hermes_webhook_token }}|$token|" \
    -e 's|{{ .resend_api_key }}|fixture|' \
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
kubectl create configmap fixture-scripts -n "$ns" --from-file=sink.py="$here/sink.py" --from-file=relay.clj="$tmp/relay.clj" --dry-run=client -o yaml
echo ---
cat "$here/fixture.yaml"
