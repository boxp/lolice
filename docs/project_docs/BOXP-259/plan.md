# BOXP-259: eMudhra 中間CA秘密鍵侵害への lolice 対応計画

- チケット: BOXP-259
- 作成日: 2026-10-21
- 対象: boxp/lolice, boxp/arch(Docker image), lolice ノード OS

## 背景

eMudhra(emSign)の中間CA 8 件の秘密鍵が弱い乱数から生成され復元可能だった(CVE-2026-111943, 報告 2026-10-17)。
親ルートは emSign ECC Root CA - G3(SHA-256 86:A1:EC:BA:…:6B)で、旧環境では Comodo AAA Services root
経由のクロス署名チェーンも成立し得る。CT ログ上に悪用リーフは確認されておらず(Google / eMudhra 声明)、
現時点で lolice が偽証明書を掴まされた痕跡はないが、「当該ルートを受理し得る」状態は残っていた。

## 判定基準

- 「TLS 検証成功 = 安全」とは扱わず、各 TLS stack が emSign ECC Root CA - G3 / Comodo AAA Services root を
  受理し得るかを、trust store の **SHA-256 指紋走査** と Go バイナリの **ビルド Go 版**(1.27.3 / 1.26.10 以降は
  SPKI 拒否あり)の両方で判定する。
- 「悪用可能性(受理し得る)」と「侵害確認(実際に偽証明書を掴まされた形跡)」を分けて記載する。

## 調査結果(要約、詳細は Obsidian BOXP-259 Notes)

| 資産 | 方向 | TLS stack | 判定 | 根拠 |
| --- | --- | --- | --- | --- |
| *.b0xp.io 公開面 | inbound | Cloudflare edge | 根拠付き非該当 | Cloudflare Tunnel 終端、証明書は Google Trust Services |
| kube-apiserver / etcd / kubelet / ArgoCD 内部 | 内部 | 自己署名 private CA | 根拠付き非該当 | Web PKI を参照しない |
| ノード OS 7 台(crio / kubelet / apt) | outbound | OS OpenSSL + Go | 影響あり → **反映済み** | ca-certificates 20260816ubuntu0.{22.04,24.04}.1 へ更新し指紋走査で不在確認 |
| ArgoCD v3.5.6 | outbound | Go crypto/x509 | 根拠付き非該当 | go1.26.10 ビルド + Ubuntu 26.04.1 修正版 bundle |
| cloudflared 2026.10.2 | outbound | Go | 根拠付き非該当 | go1.27.3 ビルド + bundle に G3 なし |
| tailscale proxy(tailscale/tailscale v1.104.1, ts-argocd-server) | outbound | Go | 影響あり → **本 PR で v1.104.2 へ**(go1.27.3 + Alpine ca-certificates 20261020-r0、bundle 118 件に G3 / Comodo AAA なしを実測) | |
| tailscale/k8s-operator v1.104.1 | outbound(control plane) | Go go1.27.1 + G3 | 影響あり・**upstream patch 待ち**(v1.104.2 image / chart 1.104.2 は 2026-10-21 時点で Docker Hub / ghcr / Helm index いずれも未公開) | |
| external-secrets v2.13.0 / argocd-image-updater v1.3.1 / alertmanager / prometheus / loki / alloy / grafana / dex / ARC 0.9.3 / longhorn-manager | outbound | Go(一部 OS bundle) | 影響あり(upstream patch 待ち、loki は v3.7.9 あり) | 旧 Go + bundle に G3 |
| codex-workspace / hermes-agent(自前 image) | outbound | OS OpenSSL / Node / Python certifi | 影響あり → **boxp/arch PR で再ビルド** | 配布済みだが未反映 |

## 変更内容(本 PR)

1. `argoproj/tailscale-operator/helm/values.yaml`: `proxyConfig.image.tag: v1.104.2` を追加し、operator が
   作成する proxy Pod(ts-argocd-server)の image を `tailscale/tailscale:v1.104.2` に固定。
   v1.104.2 image は go1.27.3 ビルドかつ Alpine ca-certificates 20261020-r0 で、bundle 118 件を SHA-256 指紋走査し
   emSign ECC Root CA - G3 / Comodo AAA Services root を含まないことを docker pull → export で確認済み。
   chart template は `PROXY_IMAGE` 環境変数に `repository:tag` を渡すため、operator Deployment の再作成と
   proxy StatefulSet の image 更新が ArgoCD 自動 sync で行われる。
2. Helm chart `tailscale-operator` の `targetRevision` は 1.104.1 のまま(初版 PR で 1.104.2 へ上げたが、
   codex review の指摘どおり chart 1.104.2 は Helm index に存在せず tgz も 404 のため取り下げ)。
   `tailscale/k8s-operator:v1.104.2` も Docker Hub / ghcr に未公開のため、operator 本体は patch 待ち。
   公開後は Renovate の chart 更新に追従し、`proxyConfig.image.tag` の固定を外す。
3. `docs/project_docs/BOXP-259/plan.md`: 本計画書。

## 再起動要否 / 接続破壊リスク / rollback

- tailscale-operator の更新は operator Pod と ts-* proxy Pod の再作成を伴う(ArgoCD 自動 sync)。
  Tailscale 経由の ArgoCD API アクセスが数十秒断する可能性があるが、クラスタ可用性には影響しない。
- rollback: `proxyConfig.image.tag` の行を削除して sync(chart appVersion の v1.104.1 に戻る)。
- ノード OS 更新は再起動不要・接続破壊リスクなし(emSign / Comodo AAA をチェーンに持つ接続先は lolice に存在しない)。
  rollback は `/var/backups/ca-certificates.crt.BOXP-259.pre` の復元、または `/etc/ca-certificates.conf` の
  `!` を外して `update-ca-certificates`。

## 未反映・残課題

- ノード上の Go デーモン(crio go1.26.3 / kubelet go1.26.8 / golyat-4 の tailscaled go1.27.1)は起動時に
  システムルートを一度だけ読み込むため、再起動まではメモリ上の旧 bundle を保持する。crio / kubelet の
  再起動(またはノード再起動)は owner 判断。
- codex-workspace / hermes-agent の再ビルド後 rollout は argocd-image-updater が自動で行う(Recreate)。
  実行中の codex run を中断するため、BOXP-252 の graceful shutdown PR 取り込み後、または run 非実行時に
  merge すること。
- Node.js 同梱ルート(codex-workspace の Node 24.22.0): upstream v24.22.1(2026-10-21)で修正済みだが
  NodeSource の deb はまだ 24.22.0。再ビルド時点の NodeSource 版に依存する。
- upstream patch 待ちの Go 製コンポーネントは Renovate PR の取り込みで順次反映する。
  tailscale/k8s-operator は v1.104.2 image と chart 1.104.2 の公開待ち(2026-10-21 時点で未公開)。
