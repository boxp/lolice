# BOXP-17: Codex workspace WARP client

Codex workspace から `even-g2-main.b0xp.io` を、`even-g2-main.even-g2-lab.svc.cluster.local` 直通ではなく Cloudflare WARP 利用者と同じ private hostname route で確認できるようにする。

## Scope

- workspace container に Cloudflare WARP enrollment 用 secret を注入する。
- workspace container に `/dev/net/tun` と `NET_ADMIN` / `NET_RAW` を付与する。
- rollout 後の実コンテナで `open tun: Operation not permitted` が出たため、workspace container は `privileged: true` にして TUN 作成を許可する。
- Calico egress policy で WARP の UDP 2408 を許可する。
- `even-g2-main` Service 側の NetworkPolicy には Codex workspace 直通許可を追加しない。

## Dependencies

- `boxp/arch` 側で codex-workspace image に Cloudflare WARP client と entrypoint 起動処理を追加する。
- `boxp/arch` 側で WARP Service Token client ID / secret / organization を AWS SSM Parameter Store に用意する。
- Cloudflare Zero Trust 側で、その Service Token を許可する device enrollment policy が必要。
