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
  - StorageClass: `codex-workspace-longhorn`, replica 2. `golyat-3` の Longhorn disk が schedulable=false のため default replica 3 では新規 volume が scheduling できない。Longhorn の最小空き率制約に合わせて初期容量は 10Gi にし、空きを作ってから拡張する。
  - SSH authorized keys: initContainer で `https://github.com/boxp.keys` から取得
  - `fetch-ssh-keys` initContainer は Longhorn PVC 上の `/home/boxp/.ssh` を更新し、`boxp` user 所有にするため、`CHOWN`/`DAC_OVERRIDE`/`FOWNER` capability だけを追加する。
  - Docker: `docker:29.5.1-cli` initContainer で CLI を配置し、`docker:29.5.1-dind` sidecar を `DOCKER_HOST=tcp://127.0.0.1:2375` で利用する
  - dind sidecar は args 先頭に `dockerd` を明示し、`docker:dind` entrypoint が既定の `0.0.0.0:2375` listener を追加して `127.0.0.1:2375` と重複 bind しないようにする。
  - Workspace container は OpenSSH privilege separation と login user への権限移行のため `SYS_CHROOT`/`SETUID`/`SETGID` capability を持つ。
  - Service: fixed LoadBalancer IP `192.168.10.98`
  - Ports: SSH `22` -> container `2222`, Even Terminal `3456`
- `boxp/arch` の `terraform/cloudflare/b0xp.io/k8s` で WARP private route `192.168.10.98/32` を追加し、既存 k8s tunnel の `warp_routing` を有効化する。
- `boxp/arch` の Cloudflare DNS に `codex-workspace.b0xp.io` DNS-only A record を追加し、`192.168.10.98` に解決させる。

## Tasks

- [x] チケットを起票する。
- [x] Even G2 Terminal Mode と `@evenrealities/even-terminal` の接続モデルを確認する。
- [x] 既存の bastion/Cloudflare/Longhorn PVC パターンを確認する。
- [x] `boxp/arch` に workspace image build と Cloudflare WARP private route を追加する。
- [x] `boxp/lolice` に codex workspace Application を追加する。
- [x] Longhorn の replica/scheduling 制約に合わせて Codex workspace PVC を replica 2 / 10Gi に調整する。
- [x] `fetch-ssh-keys` initContainer が Longhorn PVC 上で authorized_keys を更新し、owner/mode 設定できるように capability を追加する。
- [x] dind sidecar の args を明示的な `dockerd` 起動にして TCP listener の重複 bind を避ける。
- [x] WARP経由のL4到達性を優先し、ServiceをClusterIPからARK serverと同じLoadBalancer IP固定構成へ切り替える。
- [x] `sshd` の preauth `chroot("/run/sshd"): Operation not permitted` と認証後の user/group 権限移行失敗を避けるため workspace container に `SYS_CHROOT`/`SETUID`/`SETGID` capability を追加する。
- [x] VIP の port 22 が kube-vip holder node の sshd に届かないよう、Service port `22` を workspace ssh へ割り当てる。
- [x] kustomize と Terraform validate を通す。
- [x] PR を作成する。

## Verification

- `kubectl kustomize argoproj/codex-workspace`
- `kubectl kustomize argoproj`
- `kubectl kustomize argoproj/codex-workspace | kubectl --dry-run=client apply -f -`
- `kubectl get svc -A` で LoadBalancer IP `192.168.10.98` が未使用であることを確認。
