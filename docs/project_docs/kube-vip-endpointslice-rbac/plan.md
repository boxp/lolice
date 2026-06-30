# kube-vip EndpointSlice RBAC fix

## Problem

After the kube-vip image was updated to `ghcr.io/kube-vip/kube-vip:v1.2.1`, Service LoadBalancer VIPs such as `192.168.10.98` stopped being reachable.

Observed symptoms:

- The API VIP `192.168.10.99` was still reachable.
- Service VIPs such as `192.168.10.88`, `192.168.10.96`, and `192.168.10.98` did not resolve through ARP or accept TCP connections.
- `codex-workspace` itself was healthy through ClusterIP and Pod IP:
  - `10.111.250.7:22` was reachable from the `k8s` namespace probe.
  - `192.178.151.197:2222` was reachable from the `k8s` namespace probe.
- kube-vip logs showed EndpointSlice watch authorization failures:
  - `endpointslices.discovery.k8s.io is forbidden`

## Root Cause

The kube-vip ClusterRole did not grant access to `discovery.k8s.io/endpointslices`.

kube-vip v1.2.x watches EndpointSlices for Service LoadBalancer handling. Without this permission, kube-vip starts but the Service VIP watcher fails, so Service VIPs are not advertised even though the control-plane API VIP continues to work.

The issue was exposed after the kube-vip image updates:

- `v1.1.2` -> `v1.2.0`
- `v1.2.0` -> `v1.2.1`

Both updates changed only the image tag; RBAC was not updated.

## Fix

Add EndpointSlice permissions to `argoproj/kube-vip/rbac.yaml`:

- apiGroup: `discovery.k8s.io`
- resource: `endpointslices`
- verbs: `get`, `list`, `watch`, `update`

## Verification

Run:

```sh
kubectl apply --dry-run=server -f argoproj/kube-vip/rbac.yaml
kubectl auth can-i watch endpointslices.discovery.k8s.io \
  --as=system:serviceaccount:kube-system:kube-vip \
  -n monitoring
```

After the change is synced by Argo CD, restart kube-vip and verify:

```sh
kubectl -n kube-system rollout restart daemonset/kube-vip-ds
kubectl -n kube-system rollout status daemonset/kube-vip-ds --timeout=180s
kubectl -n kube-system logs -l app=kube-vip --since=5m | grep -i endpointslice
```

Expected:

- no `endpointslices.discovery.k8s.io is forbidden` errors
- Service VIP ARP is advertised
- `192.168.10.98:22` accepts TCP connections
