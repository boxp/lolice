# BOXP-223 先行PRの検証記録

確認日: 2026-10-07。指定run worktreeのみを変更した。

## Renovate

Node v24.21.0 / npm 11.19.0、公式npm package `renovate@44.142.1` を
repo外の `/tmp/boxp223-renovate` に `--ignore-scripts --no-audit --no-fund` で導入した。
repoへnode_modulesやlockfileは追加していない。

`node /tmp/boxp223-renovate/node_modules/renovate/dist/config-validator.js --strict renovate.json`
はexit 0、`Config validated successfully against 1 file(s)`。
ignore-scriptsのためnative RE2がなくRegExpへfallbackする警告あり。
追加patternは単純なアンカー・パス・エスケープ済みドットのみ。RE2固有構文は使っていない。

実packageの `dist/modules/manager/argocd/extract.js` の `extractPackageFile` を
Application全文に対して実行し、`spec.sources` から次のchartを抽出した：

```json
{
  "depName": "external-secrets",
  "registryUrls": ["https://charts.external-secrets.io"],
  "currentValue": "0.18.2",
  "datasource": "helm"
}
```

追加patternがESOのApplicationに一致し、tailscaleのApplicationに一致しないことをassertした。
最新mainの既存tailscale pattern/packageRuleを保持して追加し、両Applicationの検出を確認した。
`major.enabled` とESO packageRuleの `enabled` が未設定であること、
既存 `major.automerge=false` とESOの `automerge=false` をassertした。
全updateTypeを対象とするESOルールなので、既存の全体automerge=trueからESO更新を除外する。
major更新は無効化しない。`config:recommended` の既定動作は変更しない。

抽出結果には第二sourceのGit URL/mainも含まれる。新ルールはHelm chart名だけに一致する。
Git URLに新しい更新ルールは追加していない。
GitHub上のRenovate bot全体の実行やDependency Dashboardへの反映は未確認。
merge後にDashboard #2のchart掲載と更新PR生成を確認する必要がある。
既存open PRは#817（BOXP-218）のみでESO更新との重複はなかった。
指定worktreeの起点に別タスクの未mergeコミットがあったため、本runの変更のみ最新mainへ載せ直した。

再現例（実packageのextractorを使用。作業ディレクトリはrepo root）：

```bash
npm install --prefix /tmp/boxp223-renovate --ignore-scripts --no-audit --no-fund renovate@44.142.1
node /tmp/boxp223-renovate/node_modules/renovate/dist/config-validator.js --strict renovate.json
node --input-type=module <<'JS'
import fs from 'node:fs';
import { extractPackageFile } from '/tmp/boxp223-renovate/node_modules/renovate/dist/modules/manager/argocd/extract.js';
const file = 'argoproj/external-secrets-operator/application.yaml';
console.log(JSON.stringify(extractPackageFile(fs.readFileSync(file, 'utf8'), file, {}), null, 2));
JS
```

根拠: [公式argocd manager資料](https://docs.renovatebot.com/modules/manager/argocd/)、
[44.142.1 extractor](https://github.com/renovatebot/renovate/blob/44.142.1/lib/modules/manager/argocd/extract.ts)。

## 既存manifestと本番

`kustomize build argoproj/external-secrets-operator/manifests` と `git diff --check` は成功。
chart/Applicationと稼働manifestは変更していないため、Helm render・admission試験・CNI通信試験・
storage migration・refreshInterval 1周期の確認は未実施。
read-onlyの状態/権限確認結果と再開条件はplan.mdに記録した。
High advisoryの解消や受入条件全体の達成とは判定しない。

## CI修正patchと追加ブロッカー

terraレビューで、ESO更新PRのCIが旧版0.18.2を固定検証するP2指摘を受けた。
通常CIはApplicationのtargetRevisionを検証し、immutable preflightは既存固定版/digestとの
不一致時に停止する修正を作成したが、GitHub OAuth Appにworkflow scopeがなくpushが拒否された。
workflow変更をrevertし、未適用の修正を `ci-followup.patch` として保存した。
このPRは稼働workflowを変更しない。scopeのある承認済み経路でpatchを適用し、
CI結果を確認するまで、このP2指摘は未解消として扱う。

patchのローカル検証結果：workflow unittest 15件成功、2 workflowのactionlint成功。
yq v4.48.1で実shell部分を一時Applicationに対して実行し、通常CIは0.18.2/2.12.0を選択、
main/2.*を拒否。preflightは0.18.2だけ許可し、2.12.0/main/2.*を拒否。
これは本番のESO更新検証やGitHub上のkind CI実行を代替しない。

repo rootで `git apply --check docs/project_docs/BOXP-223/ci-followup.patch`、
`git apply docs/project_docs/BOXP-223/ci-followup.patch` で適用できる。
更新版の実装時はpreflightのESO固定版・検証済みdigestを同時更新すること。
