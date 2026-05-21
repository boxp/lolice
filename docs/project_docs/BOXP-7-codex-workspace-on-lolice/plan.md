# BOXP-7: lolice cluster 上に Codex workspace を作成する

## Goal

OpenClaw の代替として、`lolice` cluster 上に Codex と Even G2 Terminal Mode で作業できる常設 workspace を作成する。

## Requirements

- 最新 Ubuntu LTS ベース。
- Cloudflare WARP 経由で SSH 接続できる。
- Cloudflare WARP 経由で `codex-workspace.b0xp.io` hostname でも接続できる。
- GitHub の `boxp` user に登録されている public key で SSH login できる。
- Cloudflare WARP 経由で Even Realities App から `@evenrealities/even-terminal` に接続できる。
- `codex`, `@evenrealities/even-terminal`, `obsidian-headless`, `git`, `ghq`, `gwq`, `boxp/ceeker`, `bb`, `lazygit`, `yazi`, `vim`, `node`, `npm` が使える。
- Docker CLI と dind sidecar で Docker build/run が使える。
- `/home/boxp` は Longhorn PVC で永続化する。
- Secret/token は Git に直接置かない。

## Design

- `boxp/arch` で `ghcr.io/boxp/arch/codex-workspace` image を build する。
  - base: `ubuntu:26.04`
  - platform: `linux/amd64`
  - scheduler: amd64 worker へ固定
- `boxp/lolice` に `argoproj/codex-workspace` Application を追加する。
  - PVC: Longhorn, mounted at `/home/boxp`
  - SSH authorized keys: initContainer で `https://github.com/boxp.keys` から取得
  - Docker: `docker:29.1.2-cli` initContainer で CLI を配置し、`docker:29.1.2-dind` sidecar を `DOCKER_HOST=tcp://127.0.0.1:2375` で利用する
  - Service: fixed ClusterIP `10.111.250.7`
  - Ports: SSH `2222`, Even Terminal `3456`
- `boxp/arch` の `terraform/cloudflare/b0xp.io/k8s` で WARP private route `10.111.250.7/32` を追加し、既存 k8s tunnel の `warp_routing` を有効化する。
- `boxp/arch` の Cloudflare DNS に `codex-workspace.b0xp.io` DNS-only A record を追加し、`10.111.250.7` に解決させる。

## Tasks

- [x] チケットを起票する。
- [x] Even G2 Terminal Mode と `@evenrealities/even-terminal` の接続モデルを確認する。
- [x] 既存の bastion/Cloudflare/Longhorn PVC パターンを確認する。
- [x] `boxp/arch` に workspace image build と Cloudflare WARP private route を追加する。
- [x] `boxp/lolice` に codex workspace Application を追加する。
- [x] kustomize と Terraform validate を通す。
- [ ] PR を作成する。

## Verification

- `kubectl kustomize argoproj/codex-workspace`
- `kubectl kustomize argoproj`
- `kubectl kustomize argoproj/codex-workspace | kubectl --dry-run=client apply -f -`
- `kubectl get svc -A` で ClusterIP `10.111.250.7` が未使用であることを確認。
