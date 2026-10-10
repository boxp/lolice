# BOXP-226: hitohub 廃止（SQL dump 保全後に専用リソースを撤去）

チケット: Obsidian `Tickets/BOXP-226`

## Summary

サービス終了済みの hitohub (stage/prod) を廃止する。TiDB の SQL dump だけを owner 管理の非公開 S3
(`boxp-longhorn-backup`、Longhorn backup target と同じ bucket) の専用 prefix `hitohub-decommission/` へ
長期保存し、再取得・隔離 restore で検証できた後に hitohub 専用の GitOps 定義と実リソースを撤去する。

## 実測した現状 (2026-10-10)

- stage: TiDB v7.5.1、`vr_match` 9 テーブル (view/sequence なし。TiDB は trigger/routine 非対応)。
- prod: TiDB v7.5.1 だが `vr_match` DB が存在しない。`ADMIN SHOW DDL JOBS` は 2025-07-18 に system table
  のみを作成した再 bootstrap 履歴しか持たず、TiKV store は各 5 region。prod は「空 DB」として区別する。
- Longhorn volume 8 本 (stage 2、prod 6)。prod `tikv-tidb-cluster-tikv-0` のみ `numberOfReplicas: 1`
  で唯一 replica が golyat-4 上 (BOXP-194 の drain 阻害要因)。hitohub volume の Longhorn S3 backup は無い。
- Argo CD: `prod-hitohub` / `stage-hitohub` Application (automated prune/selfHeal、finalizer なし)。
  root app `argocd-apps` は `prune: false` のため、Git から消しても Application CR は自動では消えない。

## 手順

### Phase 1: 書込み停止 (この PR)

- `argoproj/hitohub/overlays/stage/deployment-{hitohub-backend,hitohub-frontend,cloudflared}.yaml` の
  replicas を 1 -> 0 にする (prod は既に 0)。
- merge 後、Argo CD automated sync で stage の backend/frontend/cloudflared が停止することを確認する。

### Phase 2: SQL dump 保全・検証 (リポジトリ変更なし、CP1 admin kubeconfig 経由の一時 namespace)

- 一時 namespace `hitohub-decommission` に ExternalSecret (SSM: TiDB root password、Longhorn backup 用 S3 credential)
  を作り、`pingcap/dumpling` で stage/prod を `--consistency snapshot` で dump、tar.gz + sha256 を
  `s3://boxp-longhorn-backup/hitohub-decommission/<UTC timestamp>/` へ保存する。
- 保存オブジェクトを再取得して hash/サイズ一致を確認し、同 namespace の隔離 TiDB (`tidb-server --store=unistore`)
  へ restore して schema/table 集合と行数を source と比較する。
- 検証成立が後続の不可逆削除の必須 gate。未成立なら停止して報告する。

### Phase 3: GitOps 撤去 (別 PR)

- `argoproj/hitohub/` 削除、`argoproj/kustomization.yaml` の 2 エントリ削除、
  `argoproj/argocd-image-updater/imageupdaters/{stage,prod}-hitohub.yaml` と kustomization エントリ削除、
  `argoproj/prometheus-operator/scrape-config.yaml` の hitohub cloudflared static target 削除、docs 更新。
- merge 後: hitohub Application に `resources-finalizer.argocd.argoproj.io` を付けて削除 (cascade)、
  残った PVC/PV (Retain)/Longhorn volume/namespace を削除、`prometheus-operator` を sync して ScrapeConfig を反映。
- `tidb-operator` (tidb-admin) は共有 operator として今回は残す。

### Phase 4: 確認・記録

- 他 namespace/サービス、API/etcd/Node/Longhorn の健全性、hitohub 残存なし、GitOps 再作成なしを確認。
- 保全台帳 (URI/サイズ/hash/復元方法/検証結果) を Obsidian `Projects/hitohub/decommission` に記録し、
  BOXP-194 へ drain 阻害解消を引き継ぐ。

## 削除しないもの

- GitHub リポジトリ、`tidb-operator`/ESO/Longhorn などの共有 operator、共有 bucket とその既存データ、
  Cloudflare zone/account、SSM の hitohub-* パラメータのうち IaC 管理外のもの (報告のみ)、
  GCS `vr-match-prod` / `vr-match-staging` (IaC 管理外、所有境界未確認のため報告のみ)。

## Phase 2 の結果 (2026-10-10、保全ゲート成立)

- 保存先: `s3://boxp-longhorn-backup/hitohub-decommission/20261010T163719Z/` (SSE AES256、Block Public Access、versioning、lifecycle なし)。
- stage: `hitohub-stage-tidb-sqldump-20261010T163719Z.tar.gz` 1,528,693 bytes、sha256 `a622dbba8eebcaefa0442329d0d2adc05e560152d8e0c342388c173f380a5626`。
  snapshot TSO 469670361563136002、`vr_match` 9 テーブル 169,611 行。
- prod: `hitohub-prod-tidb-sqldump-20261010T163719Z.tar.gz` 3,164 bytes、sha256 `46100497398f1b67750b095fc1606359a9ba9522113770d0cea58d10b1b5b8ba`。
  `vr_match` は存在せず (2025-07-18 再 bootstrap 済みの空クラスタ)、system 証跡のみ。
- 検証: S3 から再取得して sha256/サイズ一致、隔離 TiDB v7.5.1 (unistore) へ restore し table 集合 9/9・全行数が
  source snapshot inventory と一致 (sha256 同一)。
- 台帳: Obsidian `Projects/hitohub/decommission`。

## Phase 3 (この PR で削除するもの)

- `argoproj/hitohub/` 全体、`argoproj/kustomization.yaml` の prod/stage hitohub Application エントリ。
- `argoproj/argocd-image-updater/imageupdaters/{stage,prod}-hitohub.yaml` と kustomization エントリ。
- `argoproj/prometheus-operator/external-secret-alertmanager.yaml` の `namespace =~ "stage-hitohub|prod-hitohub"` null route
  (BOXP-184 で追加した hitohub 専用の通知抑止。namespace 削除後は不要)。
- `CLAUDE.md` / `docs/project-structure.md` の hitohub 記述。
- `argoproj/prometheus-operator/scrape-config.yaml` は既に BOXP-181 (PR #776) で削除済み。
