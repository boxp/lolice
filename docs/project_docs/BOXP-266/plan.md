# BOXP-266: lolice Renovate minor/patch 自動 merge を main 保護と Chainsaw/Rego CI で安全に実効化

## 目的

boxp/lolice の Renovate PR（minor/patch）を arch と同じく「required status check が全部通ったら GitHub が自動 merge」にする。
main の保護（BOXP-263 の ruleset 509073）は解除・迂回せず、required status checks を**足す**方向だけで変更する。
重い kind 統合テスト（chainsaw）の前に、数十秒で終わる静的ゲート（kustomize render + Rego）を置く。

## 調査結果（2026-10-22、live 確認）

| 項目 | 変更前 |
| --- | --- |
| `renovate.json` | `automerge: true` / `platformAutomerge: true` / `major.automerge: false` / tailscale-operator と external-secrets は `automerge: false`。設定自体は十分で、二重追加しない |
| repo `allow_auto_merge` | **false**（認証 API で確認）。このため `platformAutomerge` は効かず、Renovate 自前の automerge（次の Renovate 実行まで待つ）で merge されていた。直近 PR の作成→merge は 30 分〜8 時間 |
| ruleset 509073 | active。deletion / non_fast_forward / pull_request（approval 0）。**required status checks なし**。bypass は DeployKey のみ |
| CI（PR） | gitleaks（全 PR、6〜10s）、argocd-diff（`argoproj/**`、20〜60s、情報用）、chainsaw（`argoproj/**` のうち tests/chainsaw を持つ 4 component、kind、3m0s〜5m45s）。docs-only PR では chainsaw workflow 自体が走らない |
| Rego / conftest | なし |
| arch の参照構成 | `status-check` 集約 job（`if: failure()` で needs 全体を見る）+ ruleset `required_status_checks: [status-check]` + `allow_auto_merge: true` + GitHub App で `gh pr merge --auto` |

直近 Renovate PR の check 実績（check-runs API）:

| PR | 変更 | chainsaw | 作成→merge |
| --- | --- | --- | --- |
| #863 | argocd-image-updater ref | skipped | 1h38m |
| #859 | codex-workspace docker tag | skipped | 10h11m |
| #858 | cloudflared ×12（argocd 含む） | 3m28s | 7h00m |
| #856 | argocd ref | 3m28s | 3h21m |
| #854 | cloudflared ×12 | 5m45s | 31m |
| #848 | kube-vip | 3m03s | 19m（手動） |
| #841 | kube-vip | 3m06s | 13h41m |

## 変更内容

### リポジトリ（本 PR）

1. `.github/workflows/chainsaw.yaml`（name を Manifest Validation に）
   - `on.pull_request` の `paths` フィルタを撤去。required status check は全 PR で報告される必要があるため。
   - `static-policy` job（常時）: kustomize v5.7.1 + conftest v0.71.1（sha256 検証）を入れ、`tests/workflows` の unittest、`conftest verify`（policy の unit test）、`scripts/static-manifest-check.sh`（render + `conftest test`）を実行。
   - `renovate-config-validator` job: `renovate.json` を変更した PR だけ `renovate-config-validator --strict`。
   - `chainsaw` job は従来どおり detect が選んだ component だけ kind で実行（静的ゲートと並列。直列にすると緑の PR の所要時間が延びるため）。
   - `status-check` job: `if: always()` で needs 全部を見て、`success`/`skipped` 以外（failure/cancelled）があれば失敗。ruleset の required context はこれ。
2. `policy/kubernetes/*.rego`（render 済みマニフェストと Application 向け）
   - image に tag も digest も無い（暗黙 latest）container を deny。
   - cloudflared image は `@sha256:` 必須（`renovate.json` の `pinDigests: true` と対応）。
   - Argo CD Application の Helm chart source は `targetRevision` が固定版（`X.Y.Z` / `vX.Y.Z`）。
3. `policy/kustomize/*.rego`: `kustomization.yaml` の github.com remote resource は `?ref=<固定 tag>`。
4. `scripts/static-manifest-check.sh`: app-of-apps（`argoproj/`）を render → lolice を source にする Application の path（33 件）を kustomize build / directory 収集で render → conftest。手元でも同じものが動く。
5. `tests/workflows/test_pr_status_check.py`: paths フィルタ復活、status-check の needs 漏れ、validator/静的 job の条件を unittest で固定。
6. docs: `CLAUDE.md` CI 節、`docs/project-structure.md`。

### GitHub 設定（API で反映、スナップショットは本ディレクトリ）

- ruleset 509073 に `required_status_checks` ルールを追加: contexts `gitleaks`、`status-check`（integration_id 15368 = GitHub Actions）、`strict_required_status_checks_policy: false`。既存の deletion / non_fast_forward / pull_request と bypass（DeployKey）は変更しない。
- repo `allow_auto_merge: true`。Renovate の `platformAutomerge` が PR 作成時に GitHub auto-merge を有効化し、required checks 通過直後に squash merge する。

適用順: 本 PR の CI で `status-check` が報告されるのを確認 → ruleset 更新 → allow_auto_merge → 本 PR の mergeable 状態と `viewerCanEnableAutoMerge` を確認。

## 自動化の範囲と例外

| 対象 | 自動 merge | 根拠 |
| --- | --- | --- |
| docker tag / digest、kustomize `?ref=`、Helm chart の minor/patch | する | `automerge: true` + required checks |
| major | しない | `major.automerge: false` |
| tailscale-operator chart | しない | packageRule（BOXP-259 の pin 運用） |
| external-secrets chart | しない | packageRule（BOXP-223、CRD 移行レビュー） |
| 人が作る PR | 変わらず（approval 0、required checks が通れば merge 可能） | ruleset |

## チェック一覧（PR 時）

| check | 必須 | 走る条件 | 代表時間 |
| --- | --- | --- | --- |
| gitleaks | 必須 | 全 PR | 6〜10s |
| status-check（集約） | 必須 | 全 PR | 数秒（needs 完了後） |
| static-policy | status-check 経由 | 全 PR | CI 実測は PR に追記 |
| chainsaw | status-check 経由 | argocd / external-secrets-operator / grafana-alloy / kube-vip / tests 変更時 | 3〜6 分（変動は ESO の image pull。BOXP-254） |
| renovate-config-validator | status-check 経由 | renovate.json 変更時 | 約 1〜1.5 分（npx cold） |
| argocd-diff | 任意 | argoproj 変更時 | 20〜60s |

## 計測

- 手元（kustomize 5.7.1、conftest 0.71.1、remote resource 3 件のネットワーク取得込み）: `scripts/static-manifest-check.sh` 12〜13s。内訳は render 12s（argocd base 6.8s、kube-prometheus 1.9s、image-updater 1.0s、それ以外は合計 1s 未満）、policy 0〜1s。
- `conftest verify`: 23 tests pass。`conftest test`: 1284 + 34 documents pass（現行 main で deny 0）。
- 否定テスト: cloudflared の digest を落とした render / chart `targetRevision: 1.x` の Application はそれぞれ deny される。
- CI（cold / rerun）の実測は PR 作成後に本ファイルと Obsidian に追記する。

## rollback

- ruleset: `gh api -X PUT repos/boxp/lolice/rulesets/509073 --input docs/project_docs/BOXP-266/ruleset-509073-before.json`（before は本 PR 適用前のスナップショット。name/target/enforcement/conditions/rules/bypass_actors のみ使う）。または GitHub UI で required status checks ルールだけ外す。
- repo: `gh api -X PATCH repos/boxp/lolice -f allow_auto_merge=false`。
- workflow 側は本 PR を revert。ruleset を先に戻さないと `status-check` が無い状態で全 PR がマージ不能になるので、**revert するときは ruleset を先に戻す**。
- 緊急で何かを merge したいが status-check が壊れている場合: admin（boxp）は ruleset を編集できる。bypass actor は増やさない（BOXP-263 の方針）。

## 残件

- 既存の open PR（#860 tailscale-operator）は本 PR 以前の head なので `status-check` が無く、merge 前に「Update branch」が要る（automerge 対象外の PR）。
- argocd-diff は情報用のまま。必須にするなら docs-only PR で skip される問題を status-check に寄せる必要がある。
- `require_extra_approval_for_unattributed_changes` の運用観察（BOXP-263 残件）は継続。
