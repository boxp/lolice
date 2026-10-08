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

k8sはArgoのplain directory sourceでKustomizationがない。構造比較では既存external-secretの行末tabを検証時のみtrimして解析したため、原文のPython YAML parser受理性は未検証。GitHub Argo diffでは原文のk8s sourceを処理でき、cloudflared image差分を取得した。

全resourceの構造を比較しcloudflaredのimage以外の差分なし。rendered container14件が採用imageと一致。Tunnel引数・credential参照・protocol・他container image・replica数は保持。Longhorn `--protocol http2`を保持し、prod-hitohub/stable-diffusionはreplicas=0、minecraftは新規配備しない。

## live before

Deployment 13件、稼働11件のcloudflared container Ready。container ReadyはTunnel正常性の証拠と区別する。既存監視と反映後のTunnel正常性確認は未実施。

| namespace/workload | replicas/readyReplicas | live template image | Pod imageID | capability |
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

Longhornとlatest 2件のimageIDから解決した公開binary版は下記で確認。Pod内binaryを直接実行した結果ではない。通常2026.10.0とHermesのlive imageIDは公式amd64子manifestと一致。before latest2件。全namespaceのinit/ephemeralを含むlatest=0確認は反映後。

## 反映後の受入とrollback

owner承認後にmerge/syncし、main/反映commitと確認日時をticket Notesへ記録。13 Deployment template同一image、全namespaceのPod通常/init/ephemeral cloudflared latest=0、稼働11件のrollout/readinessと既存Tunnel監視、停止2件replicas=0、architecture別imageID対応、NET_ADMINを再確認する。既存hermes-agent Degradedを今回起因と区別する。groom時prometheus-operator OutOfSyncは本runのlive snapshotではSyncedへ変化している。

問題時は本PRをGitOpsでrevertし以前のimageへ戻す。NET_ADMINはGitOps外のlive driftのため、revertだけでcapability復旧を保証しない。capabilityを別途確認し、必要な是正はowner確認後に正本で行う。credentialは変更しない。旧タグへ戻す場合も復旧後に本件を再評価する。

## Longhorn旧タグの実版・NET_ADMIN由来

Longhorn旧タグ`1517-bb29a0e19437`のregistry indexは`sha256:8acfc9e4e65e0d1e26686177a3a0c4baa3b0330c0fc6adc899c2b673bc938e77`、amd64 childはlive imageIDと一致する`sha256:062c02f16c024dae3c525da23d2b518676ba91a4e514016e712581ffe0557c43`。

公開registryのamd64 childから15 compressed layerをdigest検証してローカルへ取得し、cloudflared binaryのみ抽出して`--version`を実行した（Pod execなし）。結果: `cloudflared version 2024.3.0 (built 2024-03-20-1013 UTC)`。tag suffixは[upstream release commit](https://github.com/cloudflare/cloudflared/commit/bb29a0e19437c3baa6a6e64f44b5de769206ed18)と一致し、[公式release 2024.3.0](https://github.com/cloudflare/cloudflared/releases/tag/2024.3.0)への対応を確認。数値prefix `1517`の意味はlabels/実版出力で特定できず、版数とは扱わない。loliceでは2024-04-21導入以来残存し、2025-07-28のhttp2調整を保持する。

argocdの両DeploymentはliveでNET_ADMINを持つが、現mainとKustomize render、last-appliedにはない。`--show-managed-fields=true`で`kubectl-patch` Update managerが`capabilities.add`を所有する（cloudflared: 2025-07-27T04:33:30Z、cloudflared-api: 同04:35:05Z）。argocd-controllerはその欄を所有せず、Application ignoreDifferencesは空。Deployment対象のMutatingWebhookConfigurationはなく、MutatingAdmissionPolicy instanceも見つからなかった。managedFieldsは操作主体・patch内容・理由を保存しないため、audit証拠なしでは必要性と意図を特定できない。

**NET_ADMIN ACは未完**。本PRのimage変更だけでこのlive driftが除去される保証はない。ownerがpatchの意図/必要性を確認し、不要ならGitOps正本でcapabilityを明示制御して承認後の同期とlive検証を行う。必要なら機能・最小権限・継続理由を記録する。root権限の新規付与やlive patchは実施していない。

独立レビュー: codex-review skillで`codex review -c 'model="gpt-5.6-terra"' --uncommitted`を実施。source差分について指摘なし、公式registry digestとRenovate kubernetes抽出を別途再確認した。NET_ADMIN/live受入の完了判定ではない。

## GitHub CIとArgo diffの範囲

初回実装commit `1829f24`に対し[ArgoCD Diff Check](https://github.com/boxp/lolice/actions/runs/37600825116)は成功、gitleaksも成功。Tailscale経路で実行した[自動diffコメント](https://github.com/boxp/lolice/pull/819#issuecomment-6035073062)を確認した。

成功statusだけで全対象のlive比較成功としない。Longhornでは既存multi-source Applicationを単一local pathで扱うためChart.yaml不在のfatal error、未配備minecraftではPermissionDeniedが出ている。Dashboardでは実source `manifests`ではなく親ディレクトリを使ったlocal diffがchart resource削除などの差分を出しており、その差分は本PRのsource/render変更を表さない。workflowはexit code 2以外のfatal errorを失敗扱いしない場合がある。Hermesはimage無変更のためworkflow対象に入らない。

これらの対象は全sourceのローカルbuild/構造比較で検証し、Argoの全source live diffは未確認範囲として残す。workflow全体の修正は本PRへ混入させない。承認反映前にownerの許可済みArgo経路でmulti-source全体のdesired/live比較を確認する。

codex-review-file skillによる検証記録レビューでは、k8s行末tabの解析範囲とcapability rollbackの保証範囲を指摘され、上記の表現を修正した。

## Renovateの更新追従証拠

Renovate `44.142.1`のconfig validatorは成功。実repoの`--platform=local --dry-run=extract`により、kubernetes manager/docker datasourceがsource全14箇所の`docker.io/cloudflare/cloudflared`を認識。全件`currentValue=2026.10.0`、採用index digest、`skipReason=null`を[renovate-source-extract.json](renovate-source-extract.json)へ記録した。

専用ruleは`matchManagers=[kubernetes]`、`matchDatasources=[docker]`、抽出されたpackage名へ一致し、`pinDigests=true`、`groupName=cloudflared image updates`を設定。updateType制限はない。既存global automerge/platformAutomerge、major.automerge=false、tailscale専用ruleを保持。ImageUpdaterへの追加なし。

- 14-image fixtureで実在する旧`2026.7.3`＋旧index digestから次タグ`2026.10.0`＋採用index digestへの14更新が、単一`renovate/cloudflared-image-updates` branchへ集約されることを`--dry-run=lookup`で確認。
- 同タグ`2026.10.0`にテスト用のall-zero digestを指定したfixtureでも、`updateType=digest`の14更新が同じ単一branchへ集約された。[renovate-digest-update.json](renovate-digest-update.json)は実RenovateのpackageFiles結果から安全な欄だけを抽出した記録。fixtureはrepo/liveに反映していない。
- pinDigestsの未固定14-image fixtureでも一括digest固定を確認。既存custom regexは依存を抽出せずskipするが、kubernetes側に14件全てがあり更新の重複/欠落を認めないため変更しない。

再現コマンド（Renovate 44.142.1導入済みのrepoルートで実行）:

```sh
renovate-config-validator renovate.json
LOG_LEVEL=debug LOG_FORMAT=json RENOVATE_CONFIG_FILE="$PWD/renovate.json" renovate --platform=local --dry-run=extract
```

更新fixtureは14個のDeployment YAMLを持つ一時directoryに本repoのrenovate.jsonをコピーし、旧タグ＋旧digest又は同タグ＋all-zero digestを指定して同コマンドの`--dry-run=lookup`を実行する。public Docker Registryでlookupし、Extracted dependencies/packageFiles with updatesのcloudflared14件と更新branchを検査する。自分のcredential/tokenや全ログは公開しない。

[Chainsaw](https://github.com/boxp/lolice/actions/runs/37600825108)も成功（初回実装commit `1829f24`）。必須CIの成功と上記Argo diffの不足範囲を別々に評価する。

## latestのbefore版数対応

09:29:24 UTCの[全namespace Pod snapshot](live-pods-before.json)は11 cloudflared containerすべてamd64で、latest 2件（init/ephemeral cloudflaredは0）。live imageIDをpublic registryへdigest指定で照合し、全layer SHAを検証してローカルbinaryの`--version`のみを実行した。[live-digest-versions.md](live-digest-versions.md)に対応を記録。

- argocd/cloudflared-api: imageID `072c067d...`はindex、amd64 child `2fa795d0...`の公開binaryは`2026.9.3 (built 2026-09-24-16:19 UTC)`。
- boxp-home/cloudflared: imageID `b392761b...`はamd64 manifest、公開binaryは`2026.7.3 (built 2026-07-23-10:24 UTC)`。

取得時のlatest tagは別のindexを指すため、latest現在値をbefore実版と同一視しない。Pod imageIDがindexを返すケースもあり、runtime digestのmediaTypeを確認してarchitectureのchildへ解決する。反映後もこの対応確認が必要。新しい脆弱性重大度/C2判断は行っていない。
