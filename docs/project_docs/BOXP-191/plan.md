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
|`current_kubernetes_version` / `candidate_kubernetes_version`|各 image の期待 server `v1.36.<patch>` または `v1.37.<patch>`。候補は現行より新しい値|

image digest は実在と platform を別途確認してから入力する。workflow は tag、短縮
SHA、未入力、同一 image、同一の期待版または候補へのdowngradeを受け付けない。架空の digest をリポジトリに保存しない。
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

## 初回CIと診断追補

[初回通常CI](https://github.com/boxp/lolice/actions/runs/36394439909)は全3件実行し、
ESO/kube-vip成功、Argo CD RedisのAvailable timeoutで1件失敗、skip0だった。
原因を断定するPod/event/logがなかったため、両workflowにkind専用kubeconfig/contextを
照合してRedis診断を保存するalways stepを追加した。Secret本体は取得せず、
各kubectl request15秒・step2分に制限する。診断失敗で本番contextへフォールバックしない。
テストのskip化やtimeout緩和は行わない。

## suiteの誤用防止

`--no-cluster`はscript内のkubectlを隔離しない。レビュー補助CLIがこのflagで既存kube-vip
suiteを実行し、本番Application patchを試みた事象は既存SAのRBACでForbiddenとなり、
書込みは成立しなかった。未実施試験や成功に読み替えず、BOXP-191台帳へ記録した。

全3suiteの最初のstepで、明示された`CHAINSAW_KIND_CONTEXT`が`kind-*`であることと
kindからexportした専用kubeconfigのcontextとの一致、およびscript実効kubeconfigのAPI接続先との一致を要求する。CI以外で環境変数がない場合、内包kubectlを
呼ぶ前に停止する。これはChainsaw自身のnamespace作成等を隔離するsandboxではないため、
実行には常に本番資格情報を含まないkind専用kubeconfigが必要。レビュー補助CLIには
本番kubeconfigを継承させず、suiteの実行を許可しない。

`test_chainsaw_isolation.py`はkubectlをローカルstubに置き換え、全3suiteについて
未指定/本番context指定/不一致を拒否し、一致時だけ通すことを確認する。
全5単体テスト成功。実機への試験コマンドは実行していない。

context名だけを比較した9b2a8bfのCIは全3件をguardで拒否した。
Chainsaw0.2.15の[rest.Config保存実装](https://github.com/kyverno/chainsaw/blob/v0.2.15/pkg/utils/rest/config.go)は
script用context名を`chainsaw`へ変更するため、専用kubeconfigをkindからexportしてcontextを
検証したうえで、script内はそのAPI接続先と一致するか判定する。Chainsaw本体にもこの専用
kubeconfigを環境変数で渡し、namespace作成を含めて同じ隔離clusterへ接続する。
専用kubeconfigはartifactへ含めない。接続先不一致・空・未指定を拒否するstubテストを追加した。

## 2026-09-28 再試行時の隔離・入力検証

専用kubeconfigをexport/照合した後は、版確認・準備manifestのapply・Chainsaw実行の
全段階でそのkubeconfigを明示する。runnerの既定contextには依存しない。
現行/候補の期待版は数値で比較し、候補が現行より新しいことを要求する。
準備コマンドの接続先と不正入力はローカルstub/入力gate試験で検証し、本番へ接続しない。

再試行ローカル検証: 入力・集計・suite guard・workflow kubeconfig固定の単体10件成功。
両workflowのactionlint構文検証成功。修正差分と計画の独立静的レビューはclean。

固定kubectl v1.37に合わせ、入力の現行/候補はv1.36.x〜v1.37.xに限定する。
SHA/digest形式、同digest拒否、版上下限・昇順の実入力gateを含め、単体15件成功。
外部準備資材はArgoCDのcommit SHA、ESO chart 0.18.2のSHA256を固定し、
kustomize/kubectlは公式checksumと実ファイルhashの一致を確認して固定した。
取得時もchecksum不一致で停止し、CLI binary hashと準備manifestを証跡へ保存する。
この記録は実行資材を追跡するもので、外部registryを含む環境全体の完全再現を保証しない。

pinの照合元: [Kustomize checksums](https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/v5.7.1/checksums.txt)、
[kubectl checksum](https://dl.k8s.io/release/v1.37.1/bin/linux/amd64/kubectl.sha256)、
[ESO chart index](https://charts.external-secrets.io/index.yaml)、
[ArgoCD tag ref](https://api.github.com/repos/argoproj/argo-cd/git/ref/tags/v3.5.2)。
