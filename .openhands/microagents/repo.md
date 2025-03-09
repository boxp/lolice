---
name: repo
type: repo
agent: CodeActAgent
---

# プロジェクト指示

タスクに取り掛かるまえに、必ず以下のファイルを読んでください

- doc/project-structure.md
- doc/project-spec.md

!!必ず日本語で受け答えしてください!!

# GitHubに関する指示に対しての注意事項

## 呼び出し方

以下のような表現で呼び出されることがあります
これより下のドキュメントを参考に対応してください

- "CIの状態を確認して"
- "このPRのテスト結果を見せて"
- "ワークフローの実行結果を教えて"
- "CI失敗の原因を分析して"
- "テストが失敗した理由を説明して"

## API情報

### GitHub REST API

PR情報の取得:
```
GET /repos/{owner}/{repo}/pulls/{pull_number}
```

PRのCIステータスの取得:
```
GET /repos/{owner}/{repo}/commits/{commit_sha}/check-runs
GET /repos/{owner}/{repo}/commits/{commit_sha}/status
```

ワークフロー実行結果の取得:
```
GET /repos/{owner}/{repo}/actions/runs
GET /repos/{owner}/{repo}/actions/runs/{run_id}
GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs
GET /repos/{owner}/{repo}/actions/runs/{run_id}/logs
```

### GitHub GraphQL API

PRとその関連情報を取得:
```graphql
query {
  repository(owner: "OWNER", name: "REPO") {
    pullRequest(number: PR_NUMBER) {
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              state
              contexts(first: 100) {
                nodes {
                  ... on CheckRun {
                    name
                    conclusion
                    detailsUrl
                    summary
                    text
                  }
                  ... on StatusContext {
                    context
                    state
                    targetUrl
                    description
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

## CIステータス確認の手順

1. 現在のリポジトリとブランチを確認します
2. プルリクエスト番号を特定します（アクティブなPR、または指定されたPR）
3. PRの最新コミットを取得します
4. そのコミットのCIステータスとチェック結果を取得します
5. ステータスを分析して結果を報告します
6. 失敗している場合は、詳細なログを取得して問題を分析します

## CI失敗時の対応

一般的なCI失敗の種類と対応:

1. **テスト失敗**:
   - 失敗したテストケースを特定
   - テストコードとテスト対象のコードを確認
   - 修正提案を行う

2. **ビルド失敗**:
   - コンパイルエラーや構文エラーを特定
   - 依存関係の問題を確認
   - 環境の違いによる問題を検討

3. **リント/フォーマットエラー**:
   - コーディングスタイルの問題を特定
   - 自動修正コマンドの提案

# 検索指示の取り扱い

ユーザーから「検索」や「調べてほしい」といった指示があった際には、必ず Bing Search を利用して情報を検索してください。取得した検索結果は日本語で分かりやすくユーザーに提供するようにしてください。


4. **依存関係の問題**:
   - バージョン不一致や欠落パッケージを確認
   - 依存関係グラフの整合性を検証
