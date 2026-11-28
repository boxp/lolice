# BOXP-762: lolice の本番デリバリーを DORA 指標として Grafana で見えるようにする

現行仕様: Obsidian `Projects/lolice-dora-metrics/spec.md`（この plan は票ごとの記録で、現行仕様ではない）。

## 調査で分かった実測できるもの

| 源 | 取れるもの | 限界 |
|---|---|---|
| Argo CD `Application.status.history` | 成功した同期の revision・deployedAt（42 app） | 1 app 10 件しか残らない。health・同期の起点は残らない |
| lolice の公開 git（main・`refs/pull/*/head`） | 変更の commit・PR の最初の commit 時刻・revert | image-updater の commit はアプリ側 repo の build 時刻を持たない |
| `Incidents/Tickets/INC-*` | 5 件（全部インフラ障害） | 配備との関連欄がない |
| Prometheus | `argocd_*` なし、保持 60 日 | – |

## 方針

1. monitoring ns に収集器 `lolice-dora-metrics`（bb、既に配備済みの workspace image の digest）を置く。
   - Argo CD Application を get/list するだけの専用 SA（argocd ns の Role）。Secret・書き込みはなし。
   - lolice を blob なしで PVC に mirror（認証なし）。
   - 同期記録を台帳へ写し、Argo CD の 10 件を超えて観測期間を伸ばす。
2. 仕様どおりに配備・変更・失敗・手当てを判定し、`lolice_dora_*` を /metrics で出す。値がないとき（対象 0 件・未計測・観測が期間の一部・未観測）は状態の metric を出し、0 で埋めない。
3. Grafana ダッシュボード `lolice-dora` を configMap で配備する（既存の provisioning と同じ形）。
4. hotfix は `Hotfix-For: #N` の trailer を最小限の記録として足す（記録開始前は数えられない）。

## 確認

- `bb tests/lolice-dora/dora-test.bb`: 仕様の例から書いた期待値。実装の出力は写していない。
- 本番データで 1 回分の収集を手元で動かし、Argo CD の履歴・git と突合する。
- 配備後に Grafana のパネルの値を、/metrics と Argo CD の履歴に突き合わせる。

Lean は適用しない: 同期記録の並び → 指標という入力→出力の関数で、順序の規則は「直前の記録と比べる」だけ。直接テストで確かめる。
