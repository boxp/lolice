# BOXP-220 実装計画

1. 最新mainとsource全14箇所、live Deployment/Podを再確認する。
2. 公式2026.10.0のmulti-architecture index digestを再取得し、Longhorn旧タグのrelease対応とargocd NET_ADMINの管理元を読み取り調査する。
3. cloudflared全imageを同一タグ＋index digestへ固定し、Renovateの既存kubernetes manager/docker datasourceに専用grouping/pinDigestsを追加する。
4. Renovate validator・抽出・更新グループ検証、変更対象Kustomize buildとimage以外の差分検証、独立レビューを行いPRを作成する。
5. merge/syncはowner承認後。未反映のlive受入条件、未確認の版数/capability、既存baselineを検証記録とNotesへ残す。停止2件・minecraft未配備を維持する。

Task Board laneはrunnerが管理する。credential/Secret本文/内部endpoint/raw logは記録しない。
