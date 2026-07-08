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
- 2026-07-08 の追加権限付与後、この runner から `hermes-agent` Pod へ直接 `exec` できるようになった。

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

追加検証用に `argoproj/hermes-agent/oauth-egress-check-job.yaml` を用意し、`argoproj/hermes-agent/kustomization.yaml` の resources に含めた。この Job は Argo CD の `PostSync` hook として apply され、sync 後に hermes-agent と同じ egress policy で OAuth 関連 FQDN の到達性を確認する。

Job の Pod は以下を本番 `hermes-agent` container と揃える。

- namespace: `hermes-agent`
- image: source manifest は `ghcr.io/boxp/arch/hermes-agent:latest`。Argo CD Image Updater の kustomize image override により、Deployment と同じ resolved image に置換される。
- label: `app=hermes-agent`
- `/etc/gai.conf` ConfigMap mount

`app=hermes-agent` label を付与することで、Calico NetworkPolicy `selector: app == 'hermes-agent'` の対象として、hermes-agent Pod と同じ egress policy で検証する。

Job は次を検証する。

- `/etc/gai.conf` に `precedence ::ffff:0:0/96` があること。
- `oauth2.googleapis.com` / `accounts.google.com` / `www.googleapis.com` の `socket.getaddrinfo(..., 443, type=SOCK_STREAM)` 先頭が IPv4 (`socket.AF_INET`) になること。
- 上記 3 host へ IPv4 優先のデフォルト名前解決で TLS handshake できること。
- `https://oauth2.googleapis.com/token` へ IPv4 強制なしの Python HTTPS request で HTTP 応答が得られること。GET `/token` は OAuth token exchange としては 404 になるため、HTTP 404 を到達成功として扱う。

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

当初、この runner では以下の権限確認により Pod 内での Python 実行確認は未実施だった。

```bash
kubectl auth can-i get pods -n hermes-agent          # yes
kubectl auth can-i create pods/exec -n hermes-agent  # no
kubectl auth can-i create pods -n hermes-agent       # no
```

2026-07-08 に `hermes-agent` への `exec` 権限が付与された後、実 Pod と検証 Job で以下を直接確認した。

```bash
kubectl logs -n hermes-agent job/hermes-agent-oauth-egress-check
```

結果:

- Job は `Complete` 済み。
- logs に `oauth egress check passed` が出力された。
- Job logs では `/etc/gai.conf` に `precedence ::ffff:0:0/96  100` があり、`oauth2.googleapis.com` / `accounts.google.com` / `www.googleapis.com` の `getaddrinfo first=` がすべて `AddressFamily.AF_INET` だった。
- 同じ 3 host で TLS handshake に成功した。
- `https://oauth2.googleapis.com/token` は HTTP 404 を返し、到達性確認として成功した。

実 `hermes-agent` Pod の `/opt/data/.venv-google` でも、IPv4 優先と OAuth token endpoint 到達性を確認した。

```bash
kubectl exec -n hermes-agent deploy/hermes-agent -c hermes-agent -- \
  sh -lc 'HERMES_HOME=/opt/data /opt/data/.venv-google/bin/python - <<'"'"'PY'"'"'
import socket
import urllib.error
import urllib.request

infos = socket.getaddrinfo("oauth2.googleapis.com", 443, type=socket.SOCK_STREAM)
print("first", infos[0][0], infos[0][4])
try:
    urllib.request.urlopen("https://oauth2.googleapis.com/token", timeout=10)
except urllib.error.HTTPError as e:
    print("http_status", e.code)
PY'
```

結果:

- Python interpreter は `/opt/data/.venv-google/bin/python`。
- `socket.getaddrinfo("oauth2.googleapis.com", 443, type=SOCK_STREAM)` の先頭は `socket.AF_INET` / IPv4。
- IPv4 強制なしの HTTPS request は `http_status 404` まで到達した。

さらに、OAuth token exchange の実コードパスでネットワークエラーが消えていることを、無効な認可コードで確認した。有効な認可コードはこの runner に渡されていないため `google_token.json` の生成までは行わず、Google token endpoint から期待どおり `invalid_grant` が返ることを確認した。

```bash
kubectl exec -n hermes-agent deploy/hermes-agent -c hermes-agent -- \
  sh -lc 'cd /opt/data && HERMES_HOME=/opt/data \
    /opt/data/.venv-google/bin/python \
    /opt/data/skills/productivity/google-workspace/scripts/setup.py \
    --auth-code invalid_codex_connectivity_probe_20260708'
```

結果:

- `ERROR: Token exchange failed: (invalid_grant) Malformed auth code.`
- `Network is unreachable` / timeout / DNS error は発生しなかった。
- `/opt/data/google_token.json` は作成されなかった。

`oauth2.googleapis.com/token` への IPv4 到達性は、実 Pod から 10 回連続で確認した。

```bash
kubectl exec -n hermes-agent deploy/hermes-agent -c hermes-agent -- \
  sh -lc 'for i in $(seq 1 10); do
    curl -4 -sS -o /dev/null \
      -w "%{http_code} %{remote_ip} %{time_connect} %{time_total}\n" \
      --connect-timeout 5 https://oauth2.googleapis.com/token
  done'
```

結果は 10/10 で HTTP 404、connect time はおおむね 12-18 ms だった。

## Required Acceptance Verification

この変更は、exec 権限を持つ環境で以下が成功するまで完了扱いにしない。2026-07-08 時点で、検証 Job と実 `hermes-agent` Pod での IPv4 優先、HTTPS 到達性、無効コードによる token exchange コードパス到達までは確認済み。`curl -4` は IPv4 を強制するため、`/etc/gai.conf` によるデフォルトのアドレス選択改善の確認としては使わない。

まず Argo CD sync 後に検証 Job が実行され、hermes-agent と同じ label / namespace / ConfigMap mount / image で OAuth 関連 FQDN の到達性を確認できることを見る。

```bash
kubectl -n hermes-agent wait --for=condition=complete job/hermes-agent-oauth-egress-check --timeout=90s
kubectl -n hermes-agent logs job/hermes-agent-oauth-egress-check
```

成功条件:

- Job が `Complete` になる。
- `kubectl kustomize argoproj/hermes-agent` の出力に `kind: Job` / `name: hermes-agent-oauth-egress-check` が含まれる。
- Job Pod の image が Deployment の `hermes-agent` container image と一致する。
- logs に `oauth egress check passed` が出る。
- 各 host の `getaddrinfo first=` が `AddressFamily.AF_INET` または値 `2` の IPv4 family になる。
- 各 host で `tls=TLS...` と peer address が出る。

失敗時の追加確認:

```bash
kubectl -n hermes-agent describe job hermes-agent-oauth-egress-check
kubectl -n hermes-agent get pods -l job-name=hermes-agent-oauth-egress-check -o wide
kubectl -n hermes-agent describe pod -l job-name=hermes-agent-oauth-egress-check
```

`Network is unreachable` が出る場合は `/etc/gai.conf` ではなく、hermes-agent Pod と同じ NetworkPolicy / node / egress gateway 側の問題として扱う。`getaddrinfo first=` が IPv6 の場合は ConfigMap mount または glibc address selection の問題として扱う。

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
  sh -lc 'cd /opt/data && HERMES_HOME=/opt/data /opt/data/.venv-google/bin/python /opt/data/skills/productivity/google-workspace/scripts/setup.py --auth-code "$AUTH_CODE" && test -s google_token.json && ls -l google_token.json'
```

成功条件:

- `setup.py --auth-code` が timeout / `Network is unreachable` なしで終了する。
- `/opt/data/google_token.json` が生成され、空ファイルではない。

## Rollback

`argoproj/hermes-agent/deployment.yaml` の `/etc/gai.conf` mount と `argoproj/hermes-agent/configmap.yaml` の `gai.conf` key を削除して Argo CD sync する。
