# T-20260301-009: tailscale-operator NetworkPolicy に control-plane endpoint(6443) 許可を追加

## 背景

`tailscale-operator` Pod から Kubernetes API への通信で以下の状況が観測された:

| 宛先 | 結果 |
|---|---|
| `10.96.0.1:443` (Service ClusterIP) | timeout |
| `192.168.10.99:6443` (kube-vip API VIP) | OK |
| `192.168.10.102:6443` (control-plane node) | timeout |

kube-vip VIP (`192.168.10.99`) は DNAT で背後の control-plane ノード (`192.168.10.102`, `192.168.10.103`, `192.168.10.104`) へ転送される。NetworkPolicy で VIP のみ許可していても、DNAT 後の実パケットは endpoint IP になるため、endpoint 側のルールがないとドロップされる。

## 修正内容

`argoproj/tailscale-operator/networkpolicy.yaml` に以下の egress ルールを追加:

- `192.168.10.102/32:6443`
- `192.168.10.103/32:6443`
- `192.168.10.104/32:6443`

既存ルールとの補完関係:
- `10.96.0.1/32:443` — Service ClusterIP 経由のアクセス (維持)
- `192.168.10.99/32:6443` — kube-vip VIP 経由のアクセス (維持)
- `192.168.10.102-104/32:6443` — DNAT 後の endpoint 直接アクセス (新規追加)

## 確認手順

```bash
# tailscale-operator Pod 内から各 endpoint への疎通確認
kubectl exec -n tailscale-operator deploy/tailscale-operator -- nc -zv 192.168.10.102 6443
kubectl exec -n tailscale-operator deploy/tailscale-operator -- nc -zv 192.168.10.103 6443
kubectl exec -n tailscale-operator deploy/tailscale-operator -- nc -zv 192.168.10.104 6443
# Service ClusterIP 経由も安定するか確認
kubectl exec -n tailscale-operator deploy/tailscale-operator -- nc -zv 10.96.0.1 443
```
