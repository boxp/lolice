# BOXP-191 Chainsaw 段階 A 準備

## 目的

Kubernetes 1.37 系の本番更新を開始する前に、lolice の Argo CD、External
Secrets Operator、kube-vip を隔離した kind 環境で、現行と候補の Kubernetes
node image に対して明示的に実行・比較する。これはマニフェスト検証用であり、
実機 CRI-O、arm64、Calico、Longhorn、GPU、etcd、drain または VIP failover の
互換性を証明しない。本番 No-Go は維持する。

## 実行方法

`Chainsaw Kubernetes Upgrade Preflight` を手動実行する。次の入力はすべて必須である。

|入力|条件|
|---|---|
|`manifest_sha`|検証対象 lolice commit の 40 桁 SHA|
|`current_kind_image`|`kindest/node@sha256:<digest>` 形式の現行 image|
|`candidate_kind_image`|同形式の候補 image。現行 image と異なる digest|
|`current_kubernetes_version` / `candidate_kubernetes_version`|各 image の期待 server `v1.<minor>.<patch>`。異なる値|

image digest は実在と platform を別途確認してから入力する。workflow は tag、短縮
SHA、未入力、同一 image または同一の期待版を受け付けない。架空の digest をリポジトリに保存しない。
対象 SHA は checkout 後に再照合するため、branch の移動で検証対象が変わらない。

workflow は 2 image × 3 component の 6 job を必ず生成する。変更ファイルから対象を
推測しない。各 job は Chainsaw CLI 版、manifest SHA、image digest、run URL、テスト
件数、JUnit tests/failures/errors/skipped、期待/実測 server版、準備状態、準備manifest、Chainsaw 出力と kubectl version を artifact に保存する。
対象 test が存在しない、準備に失敗する、JUnit reportがないかtestsが0、Chainsawが失敗する、failure/error/skipまたはserver版不一致が一件でもある場合はjobを失敗とする。

## 実行前と実行後の gate

実行前に、対象 manifest SHA と image manifest/digest を台帳へ固定し、入力 digest の
amd64 利用可否を確認する。完了後は 6 job がすべて `tests > 0`、`passed == tests`、`failures == 0`、
`errors == 0`、`skipped == 0` であること、各 artifact と run URL を実行台帳に転記する。

この段階で未達の gate は、CNI/CSI の 1.37 対応と先行更新経路、upstream #141572 の
候補 patch への修正収録、実機 CRI-O/arm64/Calico/Longhorn/GPU/etcd/drain の検証、
backup/restore 演習、段階 B/C、作業窓・停止許容時間・排他・例外受容の確定である。
これらが未達の間は dispatch や本番操作を開始しない。

`kindest/node:v1.37.1` は manifest 不在で候補 digest が未確定のため、候補実行 gate は BLOCK である。存在しないtagや架空digestを代用しない。

通常 Chainsaw CI と Preflight は `helm/kind-action@v1` の kind を `v0.33.0` に固定する。
従来の `v0.31.0` は Kubernetes 1.37 に削除済みの kubeadm `v1beta3` 設定を生成して
cluster creation が失敗した。kind v0.33.0 は Kubernetes 1.36 以降で kubeadm `v1beta4` を
使用する。通常 CI の `kindest/node:v1.37.0` 実行と、候補digestを入力する Preflight は別証跡である。

## 独立レビューでの修正とローカル検証

準備処理を独立stepへ分離し、失敗時はテストを開始しない。JUnitの実testcaseから
pass/failure/error/skipを計算し、missing/malformed/zero、CLIまたはログ書込み失敗を
合格扱いしない。`result.json`と`outcomes.env`で未実行と試験失敗を区別する。
公式[report実装](https://github.com/kyverno/chainsaw/blob/v0.2.15/pkg/report/report.go)と
[JUnit例](https://github.com/kyverno/chainsaw/blob/v0.2.15/testdata/report/JUNIT-TEST.xml)を確認した。

`python3 -m unittest discover -s tests/workflows -v`の4テストは成功。
成功・skip・failure・error・欠落・空・不正XML・CLI失敗・ログ失敗を、workflow内の
実集計コードで検証した。これはKubernetes上のChainsaw実行証跡ではない。

最終workflow検証: Preflightはactionlint 1.7.7（ShellCheck込み）成功。
通常workflowは既存のShellCheck警告が残り、ShellCheckを除く構文検証は成功。
報告gateの単体テストを通常CIにも追加した。既存警告を候補試験の合格に読み替えない。
