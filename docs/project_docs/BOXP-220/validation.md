# BOXP-220 検証記録

## 基準と反映状態

- 調査日: 2026-10-07 UTC。main基準: `64534cec5e9c64d367346f009fcc96206df594f8`。
- PR内のsource afterは全14箇所同一image。Hermesは既存固定と同じためファイル差分は13箇所。
- merge・Argo sync・rolloutは未実施。liveはbefore。PR作成をlive AC達成とは扱わない。
- 比較元: Obsidian `Projects/monthly-security-audit/runbook` と `reports/2026-10-07`、`reports/2026-10-07-cron`、ticket `BOXP-217`/`BOXP-220` Notes。

## 固定版

[公式release 2026.10.0](https://github.com/cloudflare/cloudflared/releases/tag/2026.10.0)を再確認。

`docker.io/cloudflare/cloudflared:2026.10.0@sha256:9b49eed8f62806d5d45ddf59ecefb5710429598ea6d3fcccd2af938f621b2b07`

- linux/amd64子manifest: `sha256:1a50cc8893b3f0dd283805d2efcd3336210aced60a83d967fb2bbc8ab69b5f35`
- linux/arm64子manifest: `sha256:a5c66be962694a4828d54c59b9b673194a109807bcee7445ff5b8d281059cfe4`
- indexとPod imageIDの文字列一致は要求しない。反映後にPodのnode architectureと子manifest対応を再確認する。

## source/render検証

`kustomize v5.8.2 build`をmainと変更後に実行。対象: argocd、boxp-home、longhorn、bastion、even-g2-lab、kubernetes-dashboard/manifests、hitohub/overlays/{prod,stage}、minecraft/overlays/prod、stable-diffusion、prometheus-operator、hermes-agent（12 buildすべて成功）。

k8sはArgoのplain directory sourceでKustomizationがない。全YAMLを解析した（既存external-secretの行末tabは検証時のみtrim）。

全resourceの構造を比較しcloudflaredのimage以外の差分なし。rendered container14件が採用imageと一致。Tunnel引数・credential参照・protocol・他container image・replica数は保持。Longhorn `--protocol http2`を保持し、prod-hitohub/stable-diffusionはreplicas=0、minecraftは新規配備しない。

## live before

Deployment 13件、稼働11件のcloudflared container Ready。container ReadyはTunnel正常性の証拠と区別する。既存監視と反映後のTunnel正常性確認は未実施。

| namespace/workload | replicas/readyReplicas | manifest image | Pod imageID | capability |
| --- | --- | --- | --- | --- |
| argocd/cloudflared | 1/1 | `docker.io/cloudflare/cloudflared:2026.10.0` | `sha256:1a50cc8893b3f0dd283805d2efcd3336210aced60a83d967fb2bbc8ab69b5f35` | NET_ADMIN |
| argocd/cloudflared-api | 1/1 | `docker.io/cloudflare/cloudflared:latest` | `sha256:072c067d25ccbe61d46e18f0d0723255f2bb5304f7317caa95b27031520ff92c` | NET_ADMIN |
| bastion/bastion | 1/1 | `docker.io/cloudflare/cloudflared:2026.10.0` | `sha256:1a50cc8893b3f0dd283805d2efcd3336210aced60a83d967fb2bbc8ab69b5f35` | drop ALL |
| boxp-home/cloudflared | 1/1 | `docker.io/cloudflare/cloudflared:latest` | `sha256:b392761b711c0e5649d9b64e1fc9a10ba0563fa3e712ed7c26bde5cc1fbe9059` | 追加なし |
| even-g2-lab/even-g2-lab-cloudflared | 1/1 | `docker.io/cloudflare/cloudflared:2026.10.0` | `sha256:1a50cc8893b3f0dd283805d2efcd3336210aced60a83d967fb2bbc8ab69b5f35` | 追加なし |
| hermes-agent/hermes-agent | 1/1 | `docker.io/cloudflare/cloudflared:2026.10.0@sha256:9b49eed8f62806d5d45ddf59ecefb5710429598ea6d3fcccd2af938f621b2b07` | `sha256:1a50cc8893b3f0dd283805d2efcd3336210aced60a83d967fb2bbc8ab69b5f35` | drop ALL |
| k8s/cloudflared | 1/1 | `docker.io/cloudflare/cloudflared:2026.10.0` | `sha256:1a50cc8893b3f0dd283805d2efcd3336210aced60a83d967fb2bbc8ab69b5f35` | 追加なし |
| kube-dashboard/cloudflared | 1/1 | `docker.io/cloudflare/cloudflared:2026.10.0` | `sha256:1a50cc8893b3f0dd283805d2efcd3336210aced60a83d967fb2bbc8ab69b5f35` | 追加なし |
| longhorn-system/cloudflared | 1/1 | `docker.io/cloudflare/cloudflared:1517-bb29a0e19437` | `sha256:062c02f16c024dae3c525da23d2b518676ba91a4e514016e712581ffe0557c43` | 追加なし |
| monitoring/cloudflared | 1/1 | `docker.io/cloudflare/cloudflared:2026.10.0` | `sha256:1a50cc8893b3f0dd283805d2efcd3336210aced60a83d967fb2bbc8ab69b5f35` | 追加なし |
| prod-hitohub/cloudflared | 0/0 | `docker.io/cloudflare/cloudflared:2026.10.0` | `Podなし` | 追加なし |
| stable-diffusion/stable-diffusion-cloudflared | 0/0 | `docker.io/cloudflare/cloudflared:2026.10.0` | `Podなし` | 追加なし |
| stage-hitohub/cloudflared | 1/1 | `docker.io/cloudflare/cloudflared:2026.10.0` | `sha256:1a50cc8893b3f0dd283805d2efcd3336210aced60a83d967fb2bbc8ab69b5f35` | 追加なし |

latest/Longhornの実binary版と公式release対応は調査結果を下記へ追記する。通常2026.10.0とHermesのlive imageIDは公式amd64子manifestと一致。before latest2件。全namespaceのinit/ephemeralを含むlatest=0確認は反映後。

## 反映後の受入とrollback

owner承認後にmerge/syncし、main/反映commitと確認日時をticket Notesへ記録。13 Deployment template同一image、全namespaceのPod通常/init/ephemeral cloudflared latest=0、稼働11件のrollout/readinessと既存Tunnel監視、停止2件replicas=0、architecture別imageID対応、NET_ADMINを再確認する。既存hermes-agent Degraded・prometheus-operator OutOfSyncを今回起因と区別する。

問題時は本PRをGitOpsでrevertする。以前のimageとcapability状態へ戻し、credentialは変更しない。旧タグへ戻す場合も復旧後に本件を再評価する。
