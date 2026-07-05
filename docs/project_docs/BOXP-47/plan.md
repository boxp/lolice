# BOXP-47 local-llm LAN VIP

## 目的

`local-llm/llama-server` の既存ClusterIP Serviceを維持したまま、同じLAN内の `192.168.10.0/24` 端末から OpenAI compatible API に到達できるLAN向けVIPを追加する。

## 実装方針

- 既存Service `llama-server` は `ClusterIP` のまま維持する。
- LAN公開用に追加Service `llama-server-lan` を作る。
- ServiceMonitorは `app: llama-server` labelで既存Serviceを選択しているため、`llama-server-lan` のmetadataには `app: llama-server` を付けない。
- `llama-server-lan` のselectorだけ `app: llama-server` とし、既存DeploymentのPodへ転送する。

## VIP / Service

| item | value |
| --- | --- |
| namespace | `local-llm` |
| Service | `llama-server-lan` |
| type | `LoadBalancer` |
| VIP | `192.168.10.95` |
| port | `8080` |
| targetPort | `http` |
| selector | `app: llama-server` |

既存VIPとして `192.168.10.29`, `192.168.10.30`, `192.168.10.31`, `192.168.10.88`, `192.168.10.96`, `192.168.10.97`, `192.168.10.98`, `192.168.10.99` がServiceまたはkube-vip API VIPで使用中。`192.168.10.95` はこの一覧と重複せず、実行環境からの `ping -c1 -W1 192.168.10.95` では応答なしだった。

## 検証

実施済み:

```bash
kubectl -n local-llm get deploy,pod,svc,endpoints,endpointslice -o wide
kubectl -n argocd get app local-llm -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
curl -sS --max-time 10 http://llama-server.local-llm.svc.cluster.local:8080/health
curl -sS -o /tmp/models.json -w '%{http_code}\n' --max-time 20 http://llama-server.local-llm.svc.cluster.local:8080/v1/models
curl -sS --max-time 180 http://llama-server.local-llm.svc.cluster.local:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer local-llm' \
  -d '{"model":"ornith-35b","messages":[{"role":"user","content":"Return exactly OK."}],"max_tokens":8,"temperature":0}'
kubectl kustomize argoproj/local-llm >/tmp/lolice-local-llm.yaml
kubectl apply --dry-run=client -k argoproj/local-llm
```

確認結果:

- 変更前の `local-llm` Argo CD Application は `Synced Healthy`。
- 変更前の既存Service `llama-server.local-llm.svc.cluster.local:8080` は `/health` が `{"status":"ok"}`、`/v1/models` がHTTP 200。
- 変更前の既存Service経由で `ornith-35b` の `/v1/chat/completions` smoke はHTTP 200、応答本文は `OK.`。
- render結果では既存Service `llama-server` は `ClusterIP` のまま、追加Service `llama-server-lan` は `LoadBalancer` / `loadBalancerIP: 192.168.10.95` / port `8080` / `targetPort: http`。
- `kubectl apply --dry-run=client -k argoproj/local-llm` は成功し、`service/llama-server-lan created (dry run)` を確認。
- `kubectl apply --dry-run=server -k argoproj/local-llm` はこのtask workerのRBAC不足で未完了。`system:serviceaccount:codex-workspace:codex-workspace` が `local-llm` namespaceのService作成や既存resource patchを許可されていないためForbidden。
- `local-llm` namespaceにはNetworkPolicyが無く、LAN公開範囲はService VIP到達可能な `192.168.10.0/24` を前提にする。

未実施 / PR merge後に必要:

```bash
kubectl -n local-llm get svc llama-server-lan -o wide
curl -sS http://192.168.10.95:8080/health
curl -sS -o /tmp/models.json -w '%{http_code}\n' http://192.168.10.95:8080/v1/models
curl -sS http://192.168.10.95:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer local-llm' \
  -d '{"model":"ornith-35b","messages":[{"role":"user","content":"Return exactly OK."}],"max_tokens":8,"temperature":0}'
curl -sS http://llama-server.local-llm.svc.cluster.local:8080/health
kubectl -n argocd get app local-llm -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
```

## 公開範囲と認証

この変更はLAN内向けのHTTP公開で、Service自体には認証を追加しない。既存のllama-server互換APIはAuthorization headerを受け付けるが、LAN VIPではネットワーク到達性が主な境界になる。公開範囲は信頼済みLAN `192.168.10.0/24` に限定して扱い、WANや未信頼ネットワークへは直接公開しない。

将来、LAN外や共有端末から利用する必要が出た場合は、Cloudflare Access、VPN、または認証付きreverse proxy配下に移す。特に `/v1/chat/completions` は計算資源を消費するため、LAN外公開時はアクセス制御とレート制限を必須にする。

## ロールバック

`argoproj/local-llm/kustomization.yaml` から `service-lan.yaml` を外し、`argoproj/local-llm/service-lan.yaml` を削除する。既存Service `llama-server` は変更していないため、クラスタ内経路とServiceMonitorはそのまま維持される。
