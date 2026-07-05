# BOXP-45 kubernetes-dashboard 502 復旧計画

## 目的

`kubernetes-dashboard.b0xp.io` から Cloudflare Tunnel 経由で Kubernetes Dashboard の認証画面へ到達できる状態に戻す。

## 調査結果

- `curl -k -I https://kubernetes-dashboard.b0xp.io` は Cloudflare Access の `302` を返し、Cloudflare の素の `502` は再現しなかった。
- `kubectl -n kube-dashboard get pods,svc,endpoints,externalsecret,secret -o wide` は `secrets` の list 権限がなく途中で失敗したが、Pod / Service / Endpoints / ExternalSecret は確認できた。
- Dashboard 関連 Pod と `cloudflared` Pod は `Running`。
- `external-secret` は `SecretSynced / Ready=True`。
- `kubernetes-dashboard-lb` は `LoadBalancer` として `192.168.10.96` を持つが、Endpoints が `<none>`。
- Helm chart `kubernetes-dashboard` 7.1.2 は Kong proxy Service `kubernetes-dashboard-app-kong-proxy` を作成しており、こちらは `192.178.151.192:8443` の ready endpoint を持つ。
- Argo CD `kubernetes-dashboard-app` は `Synced / Healthy`。
- `boxp/arch` の Cloudflare Tunnel 設定は upstream が `https://kubernetes-dashboard-lb.kube-dashboard:443` で、チケット記載の経路と一致している。

## 原因

追加 manifest の `kubernetes-dashboard-lb` Service が古い selector `k8s-app: kubernetes-dashboard` を使っており、Dashboard chart v7 の Kong proxy Pod に一致していなかった。そのため Cloudflare Tunnel から参照される LoadBalancer Service に endpoints が作られていなかった。

## 修正内容

- `argoproj/kubernetes-dashboard/manifests/service.yaml` の selector を Kong proxy Pod の label に変更する。

## 検証

- `kubectl -n kube-dashboard get pods,svc,endpoints,externalsecret,secret -o wide`
  - Pod / Service / Endpoints / ExternalSecret は確認できた。
  - `secret` list は `system:serviceaccount:codex-workspace:codex-workspace` に権限がなく forbidden。
- `kubectl -n argocd get application kubernetes-dashboard-app -o wide`
  - `Synced / Healthy` を確認。
- `kubectl -n kube-dashboard get endpointslices -l kubernetes.io/service-name=kubernetes-dashboard-app-kong-proxy -o yaml`
  - Kong proxy の ready endpoint `192.178.151.192:8443` を確認。
- `curl -k -I https://kubernetes-dashboard.b0xp.io`
  - Cloudflare Access の `302` を確認。
- `kubectl apply --dry-run=client -f argoproj/kubernetes-dashboard/manifests/service.yaml`
  - manifest として適用可能なことを確認。
- live cluster への一時 patch は `services` の patch 権限がなく forbidden のため未実施。
- cloudflared logs と cluster 内 curl Pod による Service 疎通確認は RBAC 不足で未実施。

## ロールバック

`argoproj/kubernetes-dashboard/manifests/service.yaml` の selector を `k8s-app: kubernetes-dashboard` に戻し、Argo CD で `kubernetes-dashboard-app` を再同期する。ただしこの状態では `kubernetes-dashboard-lb` の endpoint が空に戻る。
