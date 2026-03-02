# T-20260301-008: tailscale-operator NetworkPolicy に k8s API VIP(6443) 許可を追加

## 問題

`tailscale-operator` Pod から Kubernetes API (`kubernetes.default.svc`) へのアクセスが timeout し、
`selfsubjectaccessreviews` や Secret 読み込みが失敗している。

## 原因

kube-vip が提供する API VIP `192.168.10.99:6443` への egress が NetworkPolicy でブロックされている。
既存ルールでは Service ClusterIP (`10.96.0.1:443`) のみ許可されていたが、
Pod から kube-apiserver へのトラフィックが kube-vip VIP 経路を通るケースで接続が拒否される。

## 修正内容

`argoproj/tailscale-operator/networkpolicy.yaml` の egress ルールに以下を追加:

- `192.168.10.99/32:6443` (kube-vip API VIP)

既存の `10.96.0.1/32:443` (Service ClusterIP) ルールはそのまま維持し、
両方の経路を許可する構成とする。

## 期待される復旧挙動

- tailscale-operator が kube-apiserver に正常にアクセスできるようになる
- selfsubjectaccessreviews の timeout が解消される
- Secret の読み込みが正常に動作する
