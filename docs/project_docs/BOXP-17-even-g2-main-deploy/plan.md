# BOXP-17: Even G2 main deployment on lolice

## Goal

`boxp/even-g2-lab` の main branch build を lolice cluster 上に配信し、Cloudflare WARP 経由の private hostname から Even Realities App が QR sideloading できるようにする。

## Design

- Application: `argoproj/even-g2-lab`
- Namespace: `even-g2-lab`
- Workload: nginx static image `839695154978.dkr.ecr.ap-northeast-1.amazonaws.com/even-g2-client-main`
- Service: `ClusterIP` `even-g2-main.even-g2-lab.svc.cluster.local:80`
- Access path: Cloudflare private hostname route -> Gateway initial resolved IP -> `even-g2-lab` `cloudflared` tunnel ingress -> Kubernetes service DNS
- Cloudflared token: ExternalSecret reads `even-g2-lab-tunnel-token` from SSM Parameter Store.
- Image updates: Argo CD Image Updater watches ECR newest build and writes the selected tag back to `main`.

## Tasks

- [x] Add `even-g2-lab` Argo CD Application.
- [x] Add Deployment/ClusterIP Service/NetworkPolicy for main static app.
- [x] Add dedicated `cloudflared` Deployment and ExternalSecret in `even-g2-lab`.
- [x] Add ImageUpdater resource for ECR image updates.
- [x] Validate YAML manifests.
- [ ] After merge/apply, confirm `regcred` exists in `even-g2-lab` namespace.
- [ ] Confirm `even-g2-lab` `cloudflared` can route `even-g2-main.b0xp.io` to `http://even-g2-main.even-g2-lab.svc.cluster.local:80`.
- [ ] After first image push, confirm ImageUpdater updates the image tag.
