# BOXP-690: 使っていない tidb-operator を GitOps とクラスタから撤去する

## 目的

Application `tidb-operator` が `Unknown` になっている (chart 取得先 `charts.pingcap.org` の DNS 失敗)。
Owner の判断で修復はせず、使っていない tidb-operator を撤去する。

## 依存確認の結果 (2026-11-24 06:40〜06:50 UTC、codex-workspace SA の読み取り + CP1 admin kubeconfig の読み取り)

- `pingcap.com` の CRD 10 種 (TidbCluster / Backup / Restore など) は、全 namespace で CR が 0 件。
- TiDB / TiKV / PD / pingcap のイメージを使う Pod は `tidb-admin/tidb-controller-manager` だけ。
- `tidb-admin` namespace の中身は operator 専用の Deployment / ReplicaSet / Pod / ServiceAccount / Endpoints /
  Lease 2 個 (`tidb-controller-manager`, `tidb-scheduler`) だけ。PVC は無い。
- クラスタ全体の資源: ClusterRole / ClusterRoleBinding `tidb-operator:tidb-controller-manager`、
  自動登録された APIService `v1alpha1.pingcap.com` (CRD を消すと自動で消える)。
  webhook、PriorityClass、StorageClass、tidb 関連の PV は無い。
- 監視: PrometheusRule / ServiceMonitor / PodMonitor / ScrapeConfig / ConfigMap に tidb への参照は無い。
- CRD は Argo CD の管理外 (2024-04-20 に手で作成、追跡ラベル無し、conversion webhook 無し、finalizer 無し)。
- `pingcap.com` を参照する RBAC は operator 自身と `codex-workspace-read` (読み取りのみ) だけ。
- hitohub の TidbCluster は BOXP-226 (2026-10-10) で撤去済み。SQL dump は
  `s3://boxp-longhorn-backup/hitohub-decommission/20261010T163719Z/` に保全済みで、今回は触らない。
- 根 Application `argocd-apps` は `prune: false`。`tidb-operator` Application には finalizer が無いので、
  消しても配下の資源は連鎖削除されない。

## 変更内容 (この PR)

- `argoproj/tidb-operator/` (application.yaml, values.yaml) を削除。
- `argoproj/kustomization.yaml` から `tidb-operator/application.yaml` を削除。
- `argoproj/k8s-ecr-token-updater/cronjob.yaml` の `EXCLUDE_NAMESPACES` から `tidb-admin` を削除。
- `argoproj/codex-workspace/rbac.yaml` の `codex-workspace-read` から `pingcap.com` を削除 (CRD 削除後は不要)。
- `CLAUDE.md`、`docs/project-spec.md`、`docs/project-structure.md` の tidb-operator 記述を削除。

## merge 後のクラスタ作業 (CP1 admin kubeconfig、BOXP-226 と同じ手順)

1. `argocd-apps` が merge 後の commit で Synced になり、`tidb-operator` Application が孤児になったことを確認する。
2. Application `argocd/tidb-operator` を `--cascade=orphan` で削除する。
3. Deployment / ServiceAccount `tidb-admin/tidb-controller-manager`、ClusterRoleBinding、ClusterRole
   `tidb-operator:tidb-controller-manager` を削除する。
4. 削除直前に全 `pingcap.com` CR が 0 件であることを再確認してから、CRD 10 種を削除する。
5. operator 専用の namespace `tidb-admin` を削除する。
6. 確認: Application / Deployment / Pod / namespace / CRD / ClusterRole(Binding) / APIService が残っていないこと、
   根 app の再同期後も再作成されないこと、他 Application と Node の状態が変わっていないこと。

## 触らないもの

- PVC / PV / DB データ、保全済み SQL dump とバックアップ (S3)、共有 namespace、他アプリ。
- finalizer の一括解除や namespace の強制 finalize はしない。止まったら止めて報告する。
- arch リポジトリの `docs/project-structure.md` に残る tidb-operator 記述 (別リポジトリの文書のみ。報告だけ)。

## spec-first について

設定とリソース撤去だけで、新しい振る舞いや状態遷移のルールは無いので、spec / Lean / テストの手順は対象外。
確認は kustomize build、CI、撤去後のクラスタ状態で行う。
