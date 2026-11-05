# BOXP-390: OAuth egress PostSync Job の限定撤去

## 目的と範囲

PR #712 / #713 で導入した `hermes-agent-oauth-egress-check` は IPv4 優先・DNS・TLS・token endpoint の GET 404 を確認する検査で、OAuth 認証成功を検証しない。Owner 承認に従い、この Job と Kustomization の resource 参照だけを撤去する。旧 BOXP-63 文書には Job 運用手順の廃止を明記する。

Deployment、ConfigMap、ExternalSecret、IPv4 設定、NetworkPolicy、OAuth 連携、資格情報、天気処理は変更しない。新しい恒久監視は追加しない。Hermes の Pod template 変更・再起動が必要なら範囲を拡張せず相談する。

## 実施手順

1. 最新 main、Argo revision、同期中操作、対象 Job の UID/owner/追跡 annotation と関連 Pod、Hermes Deployment/Pod の UID/template を記録する。BOXP-389/387 の最新 Notes と実行中操作を確認し、他作業を強制停止しない。
2. Job 定義と resource 参照を削除し、最新 main と変更後の Kustomize render を構造比較する。対象 Job 1 件だけが減り、残る全リソースが一致することを確認する。
3. P0/P1 レビューと通常 CI を確認し、最新 main との差分と Hermes template 不変を再確認して通常マージする。
4. GitOps の対象 revision と追跡情報を readback する。終了済み hook の TTL3600 を優先し、通常 prune と同じ削除挙動を仮定しない。Job と UID で所有関係を確認した関連 Pod の不在、再作成なしを観測する。
5. Hermes UID/template、IPv4 ConfigMap、ExternalSecret spec、NetworkPolicy 不変を確認し、証拠と merged SHA を成果入口へ記録する。

## 残存時の扱い

対象が残存し手動削除が必要なら、active operation がないことと UID/owner を再確認し、可能なら UID precondition を使って対象だけを正規操作で削除する。worker に delete 権限がなければ principal/resource/verb を記録し、admin credential 転用や新 RBAC は行わない。無関係リソースや finalizer をまとめて削除しない。

Argo health と過去の operation result は分けて報告する。OpenWeather ExternalSecret の別障害は今回の成功条件を偽装するために変更しない。秘密値・認証ファイル・ENV 値は取得表示しない。

## 成果入口

Obsidian: `Projects/hermes-oauth-egress-check-retirement/owner-summary.md`
