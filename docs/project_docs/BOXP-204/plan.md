# BOXP-204 TD-02: 認証期限通知に再認証ページへのリンクを付ける

仕様（正本）: Obsidian `Projects/codex-task-board-runner/auth-expiry-prevention/reauth-link-spec.md`

## 原因

agent-auth の通知本文に `https://codex-auth.b0xp.io` を書いていたが、Hermes の `alertmanager_relay.clj` は
外部由来の本文中 URL を「(URL省略)」へ置き換えるため、家と Telegram にリンクが届いていなかった
（2026-11-25 15:54 JST の AgentAuthReloginDue claude の配送写しで確認）。

## 方式

- 各 provider の公式ログイン URL は都度の一時コード入りで固定ページが無い。実在の固定入口は BOXP-634 の
  本人専用ページ `https://codex-auth.b0xp.io/`（Cloudflare Access で Owner のみ。未認証は Access ログインへ 302）。
- `agent-auth-expiry-rules.yaml`: 本人ログインで解消し得る 6 ルールに annotation `reauth_url` を追加。
  observation_stale には付けない。本文は「下の再認証リンク」と案内（URL は書かない）。
- `alertmanager_relay.clj`: `reauth_url` が許可リスト（完全一致）のときだけ、発生中のアラートに
  「再認証: URL」行を出す。解除・許可外（クエリ/別パス/別ホスト/http）は出さない。本文 URL の省略は維持。
- Hermes Pod は init container で script を PVC へ写すため revision annotation を bump。
- 検知条件・帯・for・repeat・静音時間・dedupe は不変。ログインは本人が行う。

Lean モデルは作らない: 状態や順序の規則ではなく 1 payload → 文面の入出力関数で、例テストで覆える。

## テスト

- `tests/hermes-agent/alertmanager-relay-reauth-test.bb`（仕様の例表から期待値。実装前に失敗を確認）
- promtool `agent-auth-expiry-rules.test.yaml`（reauth_url を外すと失敗する負例も確認）

## 反映・確認

merge → Argo CD（prometheus-operator / hermes-agent）同期 → Prometheus のルール annotation と Hermes 内 relay の
文面を確認 → 次の実通知（claude の 72h 帯が発火中）の配送写しに「再認証:」行があることを確認。

## ロールバック

この PR を revert（revision annotation も戻る）。認証情報・検知には影響しない。
