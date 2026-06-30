# Remove codex-workspace in-pod WARP client

## Problem

`codex-workspace` was unreachable through its kube-vip LoadBalancer VIP `192.168.10.98`.

After fixing kube-vip EndpointSlice RBAC, the VIP was advertised again, but `192.168.10.98:22` still failed while other Service VIPs worked.

## Findings

- kube-vip advertised `192.168.10.98`; ARP resolved successfully.
- The `codex-workspace` Service and EndpointSlice were healthy.
- Packets from `shanghai-*` nodes reached the `golyat-3` host and were forwarded to the `codex-workspace` Pod veth.
- The Pod did not emit SYN-ACK for those connections.
- A same-node temporary Pod with comparable Calico policy was reachable from `shanghai-2`, so the failure was specific to the `codex-workspace` Pod network namespace.
- Running `warp-cli disconnect` inside the `codex-workspace` workspace container immediately restored:
  - `192.168.10.98:22`
  - `192.168.10.103:31540`
  - cloudflared-to-`192.168.10.98:22`
  - Windows WARP-to-`192.168.10.98:22`

## Root Cause

The Cloudflare WARP client running inside the `codex-workspace` Pod changed the Pod network namespace in a way that broke replies for inbound cluster/LAN traffic.

WARP is only needed on the client side for this access path:

```text
WARP client -> Cloudflare -> k8s cloudflared -> codex-workspace Service
```

Running an additional WARP client inside the target Pod is harmful for this workload.

## Fix

Remove in-pod WARP startup inputs from `codex-workspace`:

- remove WARP environment variables
- remove the WARP ExternalSecret
- remove `/dev/net/tun` hostPath and mount

The image may still contain `cloudflare-warp`, but the Pod will no longer start or configure the client because `CLOUDFLARE_WARP_ENABLED` is absent and defaults to disabled.

## Verification

After Argo CD sync:

```sh
kubectl -n codex-workspace rollout status deployment/codex-workspace --timeout=240s
kubectl -n codex-workspace exec deploy/codex-workspace -c workspace -- sh -c 'pgrep -a warp-svc || true; warp-cli status || true; ip rule'
```

Expected:

- no `warp-svc` process
- no WARP policy routing rule
- `192.168.10.98:22` succeeds from WARP client side
- `cloudflared` Pod can connect to `192.168.10.98:22`
