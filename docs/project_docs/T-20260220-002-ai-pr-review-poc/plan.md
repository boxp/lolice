# AI PRレビュー＆自動マージ検証（PoC）計画書

## 1. 背景・目的

本PoCは、loliceリポジトリにおけるPRレビュー・マージプロセスの一部をAIで自動化し、開発効率を向上させることを目的とする。

### 現状の課題

- Renovateによるイメージ更新PRなど、定型的なPRも手動レビュー・手動マージが必要
- Claude Code GitHub Actions（`@claude`メンション）が利用可能だが、レビュー→承認→マージまでの一連のフローは自動化されていない
- `gh pr merge` はgh-wrapperポリシーで拒否されており、OpenClawエージェントからの自動マージは不可

### 参考事例: GMOペパボのAI PRレビュー

GMOペパボでは以下のようなAIコードレビュー自動化を実施している（公開事例ベース）:

- **GitHub Actions + LLM連携**: PRのdiffをLLM（Claude/GPT等）に送信し、自動でレビューコメントを生成
- **段階的導入**: 最初はRenovate/Dependabotの依存関係更新PRなど低リスクな変更から開始
- **リスク分類**: 変更内容を分析し、影響度に応じて自動マージ可否を判断
- **安全ガード**: CI全パス、AIリスク判定Low、特定ラベル付与を条件とした段階的な自動化

本PoCはこれらの事例を参考に、loliceリポジトリの運用に適した形で設計する。

## 2. PoC対象範囲

### 対象（In-Scope）

| 対象 | 説明 |
|------|------|
| Renovate PRs（openclaw以外） | Docker依存関係更新。現在もrenovate.jsonで`automerge: true`設定だが、AIレビューを追加 |
| Renovate PRs（openclaw） | openclawイメージ更新。現在`automerge: false`。AIレビュー後の条件付き自動マージを検証 |
| docs/ 配下の変更PR | ドキュメントのみの変更。低リスクなため自動マージ候補 |

### 非対象（Out-of-Scope）

| 対象 | 理由 |
|------|------|
| argoproj/ 配下のインフラ変更PR | K8sマニフェスト変更は影響が大きく、人間レビュー必須 |
| 新規アプリケーション追加PR | アーキテクチャ判断が必要 |
| セキュリティ関連変更 | External Secrets、NetworkPolicy等は人間レビュー必須 |
| 本番でのauto-merge有効化 | PoCの範囲外。検証結果に基づき別途判断する |

## 3. AIレビュー＆自動マージフロー

### 3.1 全体フロー

```
PR作成
  │
  ├─ [既存] gitleaks.yaml → シークレットスキャン
  ├─ [既存] argocd-diff.yaml → argoproj/ diff表示
  │
  ▼
[新規] ai-pr-review.yaml（GitHub Actions）
  │
  ├─ Step 1: PR分類（Classification）
  │   ├─ 変更ファイルパス解析
  │   ├─ 変更行数・ファイル数の集計
  │   └─ LLMによるdiff解析・リスク判定
  │
  ├─ Step 2: リスク判定（Risk Assessment）
  │   ├─ LOW  → 自動マージ候補
  │   ├─ MED  → AIレビューコメント投稿、人間レビュー推奨
  │   └─ HIGH → 人間レビュー必須、AIレビューコメント投稿
  │
  ├─ Step 3: 自動マージ判定（Auto-merge Gate）
  │   ├─ リスク判定がLOW
  │   ├─ CI全テストパス
  │   ├─ gitleaksパス
  │   ├─ 除外条件に該当しない
  │   └─ → 全条件を満たせばApprove + auto-merge
  │
  └─ Step 4: 監査ログ出力
      ├─ 判定理由のPRコメント投稿
      ├─ 構造化ログ出力（GitHub Actions artifacts）
      └─ Slack/Telegram通知
```

### 3.2 AI分類ロジック

#### 分類の軸

| 軸 | LOW | MEDIUM | HIGH |
|----|-----|--------|------|
| 影響度（Impact） | ドキュメント、テスト、CI設定 | ConfigMap値変更、軽微なスクリプト修正 | Deployment変更、NetworkPolicy、Secrets |
| 波及範囲（Blast Radius） | 単一ファイル、単一アプリ | 複数ファイル（同一アプリ内） | 複数アプリ横断、共通基盤 |
| リスク（Risk） | 既知パターン（Renovate定期更新等） | 設定値変更（ポート、リソース制限等） | 権限変更、ネットワーク変更、新規リソース |

#### ヒューリスティック分類（LLM呼び出し前の事前フィルタ）

```yaml
# パスベースの自動分類ルール
auto_low:
  - "docs/**"
  - "*.md"
  - ".github/workflows/gitleaks.yaml"

auto_high:
  - "argoproj/*/networkpolicy*.yaml"
  - "argoproj/*/secret*.yaml"
  - "argoproj/*/externalsecret*.yaml"
  - "configmap-gh-wrapper.yaml"

renovate_low:
  # Renovateボットによる以下の変更
  - ".argocd-source-*.yaml"  # イメージタグ更新のみ
  - "renovate.json"
```

#### LLMによる詳細分析

ヒューリスティックで分類できない場合、LLM（Claude API）にdiffを送信し以下を判定:

- 変更の要約（1-2文）
- 影響度: LOW / MEDIUM / HIGH
- 波及範囲: SINGLE_APP / MULTI_APP / INFRASTRUCTURE
- リスク要因: 具体的なリスクポイントの列挙
- 自動マージ推奨: YES / NO（理由付き）

#### 機密情報保護ポリシー（LLM送信前）

LLMへのdiff送信にあたり、以下の機密情報保護措置を実施する:

1. **送信前の必須チェック**: gitleaksによるシークレットスキャンが**完了・パス済み**であることをLLM送信の前提条件とする。gitleaks未完了またはfailの場合はLLM送信を行わない
2. **送信禁止パス**: 以下のファイルのdiffはLLMに送信しない（ヒューリスティックでHIGH分類し、人間レビューにエスカレーション）
   - `argoproj/*/secret*.yaml`
   - `argoproj/*/externalsecret*.yaml`
   - `.env*`, `*credentials*`, `*token*`
3. **機密マスキング**: diff送信前に、既知のシークレットパターン（Base64エンコード済みSecret値、API Key形式の文字列等）を`[REDACTED]`に置換
4. **LLMログの保存制限**: GitHub Actions artifactsに保存するLLMログは**要約のみ**（分類結果、リスク判定、判定理由）とし、diff原文やプロンプト全文は保存しない

### 3.3 自動マージ条件（Approve Gate）

以下の**全条件**を満たす場合にのみ自動マージを実行:

| # | 条件 | 検証方法 |
|---|------|----------|
| 1 | AI分類がLOW | ai-pr-review ワークフロー内で判定 |
| 2 | CI全テストパス | GitHub Actions status check |
| 3 | gitleaksパス | gitleaks.yaml status check |
| 4 | PRラベルに`auto-merge-blocked`が**ない** | GitHub Labels API |
| 5 | 変更ファイル数 ≤ 5 | GitHub Files API |
| 6 | 変更行数（additions + deletions）≤ 100 | GitHub PR API |
| 7 | PRの作成者がbot（renovate等）または許可リスト内 | PR author check |

### 3.4 除外条件（自動マージしない場合）

以下のいずれかに該当する場合、自動マージを行わず人間レビューにエスカレーション:

- AI分類がMEDIUM以上
- `argoproj/*/deployment*.yaml` の変更を含む（イメージタグのみの`.argocd-source-*.yaml`は除く）
- `argoproj/*/networkpolicy*.yaml` の変更を含む
- ExternalSecret/Secret関連ファイルの変更を含む
- `configmap-gh-wrapper.yaml` の変更を含む（セキュリティポリシー）
- `kustomization.yaml`（App-of-Apps）の変更を含む
- `.github/workflows/`配下のワークフローファイルの変更を含む（自動マージ条件のバイパス防止）
- PRに`auto-merge-blocked`ラベルが付与されている
- 人間がPRにchanges-requestedレビューを投稿済み

## 4. 安全ガード

### 4.1 多層防御

```
Layer 1: ヒューリスティック分類（パスベース、即時判定）
Layer 2: LLMリスク分析（diff内容ベース）
Layer 3: 定量フィルタ（ファイル数、行数制限）
Layer 4: CI/CDゲート（テスト、gitleaks）
Layer 5: 人間オーバーライド（ラベル、レビュー）
```

### 4.2 Circuit Breaker

自動マージの異常を検知して自動停止する仕組み:

| トリガー | 検知方法 | アクション |
|----------|----------|-----------|
| 自動マージ後24時間以内にrevert PRが作成された | `pull_request`イベントで新規PRを検知し、PRボディの`Reverts #<number>`参照またはgit revertメタデータ（`This reverts commit <sha>`）で因果紐付け。revert対象PRのマージイベントが`github-actions[bot]`であることを確認。タイトルのみの判定は行わない | 自動マージ機能を一時停止（48時間） |
| 1週間以内に自動マージ後のArgoCD sync失敗が2回以上 | ArgoCD Notifications（Webhook）経由でsync failure eventを受信。失敗したApplication名とマージされたPRの変更パスを照合し因果紐付け | 自動マージ機能を一時停止（1週間） |
| 管理者がRepository Variableを`tripped`に設定（手動停止） | ワークフロー実行時にRepository Variable `AUTO_MERGE_CIRCUIT_BREAKER` をチェック | 手動解除まで全自動マージ停止 |

#### Circuit Breaker状態管理

- **状態保存先**: GitHub Repository Variables（`AUTO_MERGE_CIRCUIT_BREAKER`）
  - 値: `open`（正常）/ `tripped:<ISO8601 timestamp>:<reason>`（発動中）
- **チェックタイミング**: `ai-pr-review.yaml`ワークフローの最初のステップで状態を読み取り、`tripped`の場合はマージ判定をスキップ
- **自動解除**: `tripped`のタイムスタンプから指定期間経過後、次回ワークフロー実行時に`open`に戻す
- **手動確認フロー**: Circuit Breaker発動時はTelegram通知を送信し、チームメンバーが発動原因を確認。誤発動の場合はGitHub Repository Variablesを手動で`open`に更新して解除

### 4.3 ロールバック手順

自動マージされたPRに問題が発生した場合:

1. **即座の対応**: `git revert`でrevert PRを自動生成（ワークフローに組み込み）
2. **ArgoCD自動Sync**: loliceはautoSync有効のため、revert PRマージ後に自動で前の状態に復元
3. **通知**: Telegram/Slack通知でチームに即座に周知
4. **事後分析**: 自動マージの判定ログをもとに、分類ロジックの改善点を特定

## 5. 必須監査項目

### 5.1 ログ要件

| 項目 | 保存先 | 保存期間 |
|------|--------|----------|
| AI分類結果（影響度、波及範囲、リスク） | **正本**: GitHub Actions artifacts（ハッシュ付きJSON）、**補助**: PRコメント（可視化用） | 90日（artifacts）。PRコメントは利便性のみ |
| 自動マージ判定理由（全条件の結果） | **正本**: GitHub Actions artifacts（ハッシュ付きJSON）、**補助**: PRコメント（可視化用） | 90日（artifacts）。PRコメントは利便性のみ |
| LLM API応答要約（分類結果・判定理由のみ、diff原文は含まない） | GitHub Actions artifacts | 90日 |
| マージ実行者（bot/human） | GitHub PR merge event | 永続（Git履歴） |
| Circuit Breaker発動/解除イベント | GitHub Issue + Telegram通知 | Issue存続期間 |

### 5.1.1 監査証跡の再現性・改ざん耐性

すべての判定記録には以下の再現性情報を必須で含める:

| 再現性情報 | 説明 |
|-----------|------|
| ルールセットID | ヒューリスティック分類ルールのバージョン（Git commit SHA） |
| プロンプトバージョン | LLM送信に使用したプロンプトテンプレートのバージョン |
| モデルID | 使用したLLMモデルの完全な識別子（例: `claude-sonnet-4-20250514`） |
| 入力ハッシュ | LLMに送信したdiff内容のSHA-256ハッシュ（再現性検証用） |
| 対象コミットSHA | PRのhead commit SHA |

**監査証跡の保存先と改ざん耐性**: PRコメントは投稿者や権限保持者が編集・削除できるため、改ざん耐性のある監査証跡としては不十分である。PRコメントは**可視化・利便性の目的**に限定し、監査の正本は以下の改ざん耐性のある保存先に置く:

- **GitHub Actions artifacts**: 判定結果の構造化JSON（SHA-256ハッシュ付き）を90日間保存
- **外部監査ログ**: 長期保存が必要な場合は、書き込み専用の外部ログストア（例: append-onlyなCloud Storage、SIEMへの転送）に出力

PRコメントには「正本はartifactに保存済み（Run ID: xxx）」とリンクを記載し、参照先を明示する。

### 5.2 承認トレーサビリティ

すべてのAI自動マージには以下が記録される:

- **誰が承認したか**: `github-actions[bot]`（AI自動承認）であることを明示
- **どのモデルが判定したか**: 使用したLLMモデル名・バージョン
- **なぜ承認したか**: AI分類結果と全条件チェック結果
- **いつ承認したか**: タイムスタンプ（UTC）

PRコメントには以下のフォーマットで投稿:

```markdown
## AI Auto-merge Review

**Model**: claude-sonnet-4-20250514
**Classification**: LOW
**Impact**: Documentation only
**Blast Radius**: SINGLE_APP
**Risk Factors**: None identified

### Condition Checks
- [x] AI Classification: LOW
- [x] CI: All checks passed
- [x] Gitleaks: Passed
- [x] No `auto-merge-blocked` label
- [x] Changed files: 1 (≤ 5)
- [x] Changed lines: 12 (≤ 100)
- [x] Author: renovate[bot] (allowed)

**Decision**: AUTO-MERGE APPROVED
**Timestamp**: 2026-02-20T12:00:00Z
```

### 5.3 失敗時停止

| 障害ケース | 対応 |
|-----------|------|
| LLM API呼び出し失敗 | 自動マージをスキップし、人間レビューにフォールバック |
| GitHub Actions ワークフロー失敗 | 自動マージは実行されない（フェイルセーフ） |
| 分類結果が不明確（LLMがLOW/MED/HIGHを明確に返さない） | HIGHとして扱い、人間レビューにエスカレーション |
| Circuit Breaker発動中 | 全自動マージ停止、人間レビューのみ |

## 6. 実装計画

### Phase 0: 本PoC計画の承認（本PR）

- 計画書のレビュー・承認
- PoC実施の合意形成

### Phase 1: AIレビューワークフロー追加（観測のみ）

- `ai-pr-review.yaml` ワークフローを追加
- **自動マージは行わない**: 分類結果をPRコメントとして投稿するのみ（監査証跡のartifact保存はPhase 1から実施する）
- 1-2週間の観測期間で分類精度を検証

### Phase 2: 自動マージの段階的有効化

- Phase 1の分類精度が十分と判断された後に実施
- 最初はRenovate PR（openclaw以外）のみ対象
- 実装方式は案A（GitHub Actions内で直接マージ）を採用し、gh-wrapperポリシーは変更しない

### Phase 3: 対象範囲の拡大

- openclaw Renovate PRへの適用
- docs/ のみ変更PRへの適用
- Circuit Breaker運用の定着確認

## 7. gh-wrapperポリシー変更の検討

現在`gh pr merge`はgh-wrapperで完全に拒否されている。自動マージを実現するためには以下のいずれかの方法が必要:

### 案A: GitHub Actions側で直接マージ（推奨）

- `ai-pr-review.yaml` ワークフロー内でGITHUB_TOKENを使って直接マージ
- gh-wrapperの変更不要
- OpenClawエージェントからの意図しないマージを防止できる

#### 案Aのセキュリティ要件

| 要件 | 対策 |
|------|------|
| ワークフロー権限の最小化 | `permissions`で`contents: write`, `pull-requests: write`のみ付与。他の権限は付与しない |
| イベントトリガーの安全設計 | `pull_request`イベントを使用（`pull_request_target`は使用しない）。**注意**: `pull_request`イベントではfork PRでもワークフロー自体は起動する（権限が制限されるのみ）ため、ジョブ条件でのfork除外を必須とする |
| fork PRの除外（必須） | すべてのジョブに `if: github.event.pull_request.head.repo.full_name == github.repository` 条件を付与し、fork PRではジョブが一切実行されないことを保証する。この条件がないジョブはfork PRでも動作し、LLM呼び出しによるコスト増や情報露出のリスクがある |
| ワークフロー改竄防止 | `.github/workflows/ai-pr-review.yaml`の変更を含むPRはHIGH分類とし、自動マージ対象外にする |
| ブランチ保護ルールとの整合 | mainブランチの保護ルール（required reviews等）が自動マージと矛盾しないよう設定を調整 |

### 案B: gh-wrapperに条件付き許可を追加

- 特定の条件（ラベル、CIステータス等）を満たすPRのみ`gh pr merge`を許可
- gh-wrapperのロジックが複雑化するリスク
- OpenClawエージェントから意図せずマージされるリスクが上がる

### 推奨: 案A

GitHub Actions内で完結させることで、gh-wrapperポリシーを維持しつつ自動マージを実現する。

## 8. リスクと緩和策

| リスク | 影響 | 緩和策 |
|--------|------|--------|
| LLMの誤分類（HIGH→LOWの見逃し） | 危険な変更が自動マージされる | 多層防御（ヒューリスティック+LLM+定量フィルタ）、除外条件の厳格化 |
| LLM APIのコスト増大 | 運用コスト増 | ヒューリスティック分類で事前フィルタし、LLM呼び出しを最小化 |
| Circuit Breaker誤発動 | 正常なPRの自動マージが停止 | 発動条件の閾値を段階的に調整、手動解除の手順を整備 |
| ワークフローファイルの改竄 | 自動マージ条件のバイパス | mainブランチ保護ルール、ワークフローファイル変更時はHIGH分類 |
| AI承認の責任所在が不明確 | インシデント時の対応遅延 | 監査ログの徹底、承認トレーサビリティの確保 |

## 9. 成功基準

| 基準 | 目標値 | 評価方法 |
|------|--------|----------|
| Phase 1 分類精度（人間判定との一致率） | ≥ 90% | 混同行列による全クラス評価 |
| 重大誤判定率（HIGHをLOWと判定するFalse Negative） | 0% | 全HIGH分類PRを人間が検証。見逃しゼロを必達目標とする |
| 自動マージ後のrevert発生率 | 0%（Phase 2初期） | revert PRの発生有無を追跡 |
| Circuit Breaker誤発動率 | ≤ 月1回 | 発動イベントログを集計 |
| PRマージまでの平均時間短縮 | 対象PR: ≥ 50%短縮 | マージまでの経過時間を対象PR群で計測 |

Phase 1の評価では、分類精度の全体一致率に加え、**混同行列（Confusion Matrix）** を用いて各分類レベル間の誤判定パターンを分析する。特にHIGH→LOW方向の見逃し（False Negative for HIGH）は最も危険な誤判定であり、発生件数ゼロをPhase 2移行の必須条件とする。
