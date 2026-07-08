# BOXP-63: hermes-agent Google OAuth IPv4 preference

## Goal

hermes-agent sandbox の Google Workspace OAuth token exchange が `https://oauth2.googleapis.com/token` へ安定して到達できるようにする。

## Findings

- `oauth2.googleapis.com` は A と AAAA の両方を返す。
- codex-workspace runner からの確認では IPv4 は安定して到達できる。
  - `curl -4 https://oauth2.googleapis.com/token`: 10/10 成功、HTTP 404。GET なので 404 は正常な到達性確認として扱う。
  - 接続先は `142.251.169.95` / `172.217.221.95`、connect time はおおむね 14-23 ms。
- 同じ runner から IPv6 は到達できない。
  - `curl -6 https://oauth2.googleapis.com/token`: 5/5 失敗。
- hermes-agent namespace の Calico NetworkPolicy は外部 TCP `443` を許可しているため、少なくとも GitOps manifest 上は Google OAuth への IPv4 HTTPS egress はブロックしていない。
- この runner の ServiceAccount には `hermes-agent` Pod への `pods/exec` 権限がないため、Pod 内の `/opt/data/.venv-google` で実 token exchange までは確認できない。

## Root Cause

主要因は、lolice cluster / sandbox に IPv6 egress route が無い状態で、Google OAuth endpoint が AAAA record を返し、Python OAuth client が IPv6 を先に試すこと。

ファイアウォール/allowlist については、Calico policy が TCP 443 を許可しており、同一 cluster 内の codex-workspace runner から IPv4 で `oauth2.googleapis.com:443` へ安定到達できるため、現時点では主因とは見なさない。

## Implemented Change

`argoproj/hermes-agent/configmap.yaml` に `gai.conf` を追加し、`precedence ::ffff:0:0/96 100` で glibc の `getaddrinfo(3)` が IPv4-mapped address を優先するようにした。

`argoproj/hermes-agent/deployment.yaml` では以下に `/etc/gai.conf` として ConfigMap key を read-only mount する。

- `hermes-agent` container
- `bootstrap-config` initContainer

OAuth token exchange は `hermes-agent` container の `/opt/data/.venv-google` から実行される想定。initContainer にも同じ mount を入れ、起動時の helper/setup が外部 HTTPS を使う場合にも同じ名前解決方針に揃える。

NetworkPolicy は既に外部 TCP 443 を許可しているため、今回の変更では allowlist 追加は行わない。

## Verification Performed

```bash
curl -4 -sS -o /dev/null -w 'v4 code=%{http_code} connect=%{time_connect} total=%{time_total} ip=%{remote_ip}\n' --connect-timeout 5 --max-time 10 https://oauth2.googleapis.com/token
```

上記を 10 回実行し、すべて HTTP 404 で接続成功した。`/token` への GET は OAuth token exchange としては不正な method なので 404 だが、TLS/HTTPS 到達性の確認としては十分。

```bash
curl -6 -sS -o /dev/null -w 'v6 code=%{http_code} connect=%{time_connect} total=%{time_total} ip=%{remote_ip}\n' --connect-timeout 5 --max-time 10 https://oauth2.googleapis.com/token
```

上記は 5 回すべて接続失敗し、IPv6 egress が使えないことを確認した。

```bash
kubectl -n hermes-agent get networkpolicies.projectcalico.org hermes-agent-network-policy -o yaml
```

外部 TCP `443` が許可されていることを確認した。

## Post-Apply Verification

Argo CD sync 後、exec 権限を持つ管理者環境で以下を確認する。

```bash
kubectl -n hermes-agent rollout status deploy/hermes-agent

kubectl -n hermes-agent exec deploy/hermes-agent -c hermes-agent -- \
  sh -lc 'cat /etc/gai.conf; getent ahosts oauth2.googleapis.com | head -20'

kubectl -n hermes-agent exec deploy/hermes-agent -c hermes-agent -- \
  sh -lc 'for i in $(seq 1 10); do curl -4 -sS -o /dev/null -w "%{http_code} %{remote_ip} %{time_connect} %{time_total}\n" --connect-timeout 5 --max-time 10 https://oauth2.googleapis.com/token; done'
```

OAuth 認可コードを取得した後、同じ Pod で以下を実行する。

```bash
kubectl -n hermes-agent exec -it deploy/hermes-agent -c hermes-agent -- \
  sh -lc 'cd /opt/data && /opt/data/.venv-google/bin/python setup.py --auth-code "$AUTH_CODE" && test -s google_token.json && ls -l google_token.json'
```

成功条件:

- `setup.py --auth-code` が timeout / `Network is unreachable` なしで終了する。
- `/opt/data/google_token.json` が生成され、空ファイルではない。

## Rollback

`argoproj/hermes-agent/deployment.yaml` の `/etc/gai.conf` mount と `argoproj/hermes-agent/configmap.yaml` の `gai.conf` key を削除して Argo CD sync する。
