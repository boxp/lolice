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
- この runner の ServiceAccount は `hermes-agent` namespace で `get pods` は可能だが、`create pods/exec` と `create pods` は不可。そのため、この runner から Pod 内の Python `getaddrinfo` や `/opt/data/.venv-google` での実 token exchange までは確認できない。

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

Argo CD sync 後、実 cluster の Pod spec と ConfigMap 反映状態を確認した。

```bash
kubectl -n hermes-agent get pods -l app=hermes-agent -o wide
kubectl -n hermes-agent get pod -l app=hermes-agent -o yaml
kubectl -n hermes-agent get cm hermes-agent-config -o yaml
```

確認結果:

- `hermes-agent-77b6498978-z8mlm` は `3/3 Running`。
- `hermes-agent` container に `config` volume の `/etc/gai.conf` read-only mount がある。
- `bootstrap-config` initContainer にも `config` volume の `/etc/gai.conf` read-only mount がある。
- `hermes-agent-config` ConfigMap に `precedence ::ffff:0:0/96  100` が入っている。

この runner では以下の権限確認により、Pod 内での Python 実行確認は未実施。

```bash
kubectl auth can-i get pods -n hermes-agent          # yes
kubectl auth can-i create pods/exec -n hermes-agent  # no
kubectl auth can-i create pods -n hermes-agent       # no
```

## Required Acceptance Verification

この変更は、exec 権限を持つ管理者環境で以下が成功するまで完了扱いにしない。`curl -4` は IPv4 を強制するため、`/etc/gai.conf` によるデフォルトのアドレス選択改善の確認としては使わない。

```bash
kubectl -n hermes-agent rollout status deploy/hermes-agent

kubectl -n hermes-agent exec deploy/hermes-agent -c hermes-agent -- \
  sh -lc 'cat /etc/gai.conf'

kubectl -n hermes-agent exec deploy/hermes-agent -c hermes-agent -- \
  sh -lc 'python3 - <<'"'"'PY'"'"'
import socket
infos = socket.getaddrinfo("oauth2.googleapis.com", 443, type=socket.SOCK_STREAM)
for info in infos[:10]:
    print(info)
first = infos[0]
if first[0] != socket.AF_INET:
    raise SystemExit(f"first address is not IPv4: {first!r}")
PY'
```

成功条件:

- `/etc/gai.conf` に `precedence ::ffff:0:0/96  100` がある。
- Python `socket.getaddrinfo("oauth2.googleapis.com", 443, type=socket.SOCK_STREAM)` の先頭が `socket.AF_INET` の IPv4 address になる。
- 先頭が IPv6 の場合は、今回の恒久対応が Python OAuth client に効いていないため不合格。

ネットワーク到達性は、IPv4を強制しない Python HTTPS request で確認する。GET `/token` は OAuth token exchange としては 404 になるが、TLS接続とHTTP応答が取れれば到達性確認として扱える。

```bash
kubectl -n hermes-agent exec deploy/hermes-agent -c hermes-agent -- \
  sh -lc 'python3 - <<'"'"'PY'"'"'
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("https://oauth2.googleapis.com/token", timeout=10)
except urllib.error.HTTPError as e:
    print("http_status", e.code)
    if e.code != 404:
        raise
PY'
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
