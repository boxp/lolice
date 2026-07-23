# BOXP-85: Hermes Agent write safe root

## 目的

Hermes Agent の main container `hermes-agent` に `HERMES_WRITE_SAFE_ROOT=/opt/data:/home/boxp/Documents/obsidian-headless/BOXP` を追加し、永続データ領域と BOXP Obsidian vault を Hermes の書き込み許可 root として明示する。

## 対象

- Repository: `boxp/lolice`
- Manifest: `argoproj/hermes-agent/deployment.yaml`
- Namespace: `hermes-agent`
- Workload: `Deployment/hermes-agent`
- Container: `hermes-agent`

## 方針

1. `hermes-agent` main container の既存 `env` に `HERMES_WRITE_SAFE_ROOT` を追加する。
2. 値は `/opt/data:/home/boxp/Documents/obsidian-headless/BOXP` と完全一致させる。
3. `obsidian-sync` container、`cloudflared` container、PVC、Cloudflare Tunnel、Cloudflare Access、LLM 接続、NetworkPolicy、RBAC は変更しない。
4. 既存の UID/GID `1000:10000`、`fsGroup: 10000`、PVC mount 設計は維持する。

## 事前確認

- `argoproj/hermes-agent/deployment.yaml` の main container `hermes-agent` には `HERMES_WRITE_SAFE_ROOT` が未設定だった。
- main container `hermes-agent` には `envFrom` がなく、同名 env を ConfigMap / Secret から一括注入する経路はなかった。
- `OBSIDIAN_VAULT_PATH` は `/home/boxp/Documents/obsidian-headless/BOXP`。
- `/opt/data` と `/home/boxp` は同じ PVC `hermes-agent-data` を mount している。

## 検証

ローカル manifest 生成:

```bash
kubectl kustomize argoproj/hermes-agent
```

生成結果の確認:

```bash
kubectl kustomize argoproj/hermes-agent >/tmp/hermes-agent.yaml
awk '
  function check_block() {
    if (block ~ /\n        name: hermes-agent\n/ &&
        block ~ /- name: HERMES_WRITE_SAFE_ROOT\n          value: \/opt\/data:\/home\/boxp\/Documents\/obsidian-headless\/BOXP/) {
      print "- name: HERMES_WRITE_SAFE_ROOT"
      print "  value: /opt/data:/home/boxp/Documents/obsidian-headless/BOXP"
    }
  }
  /^      containers:/ { in_containers = 1; next }
  in_containers && /^      volumes:/ { check_block(); exit }
  in_containers && /^      - / { check_block(); block = $0 ORS; next }
  in_containers { block = block $0 ORS }
' /tmp/hermes-agent.yaml
```

期待値:

```yaml
name: HERMES_WRITE_SAFE_ROOT
value: /opt/data:/home/boxp/Documents/obsidian-headless/BOXP
```

デプロイ後の実 Pod 確認:

```bash
kubectl -n hermes-agent rollout status deploy/hermes-agent
kubectl -n hermes-agent exec deploy/hermes-agent -c hermes-agent -- printenv HERMES_WRITE_SAFE_ROOT
```

vault 書き込み確認:

```bash
kubectl -n hermes-agent exec deploy/hermes-agent -c hermes-agent -- \
  sh -c 'tmp="${OBSIDIAN_VAULT_PATH}/.hermes-write-safe-root-check.md"; echo "BOXP-85 $(date -Iseconds)" > "$tmp"; test -s "$tmp"; rm "$tmp"'
```

## ロールバック

`argoproj/hermes-agent/deployment.yaml` の `HERMES_WRITE_SAFE_ROOT` env を削除し、Argo CD sync する。
