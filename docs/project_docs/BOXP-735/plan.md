# BOXP-735 TD-05: @b0xp2 の新しい公開投稿を毎日 18 時に通知なしで確かめる

現行仕様: vault `Projects/kohako-x-read-grok/daily-spec.md`（spec index: kohako-x-daily）。

## 方針
- Hermes cron に script だけの job（`--no-agent`、配送・失敗配送とも `local`、Asia/Tokyo `0 18 * * *`）を 1 つ登録する。
- 本体 `kohako_x_daily.py` は Hermes 内蔵の公式 `x_search` を `allowed_x_handles=["b0xp2"]` で 1 回呼ぶ。
  `XAI_API_KEY` があるとき・出所が `xai-oauth` 以外のときは呼ばない。degraded や失敗は既読を変えない。 古い控えは成否・停止に関係なく毎回（検索前に）消し、`XAI_API_KEY` で止めた回も `last_run.json` に error を書く。
- 出典にある @b0xp2 の投稿 URL だけを採り、既読（最大 500 件）を除いた新着だけを
  `/opt/data/kohako-x-read/digest/YYYY-MM-DD.md` に短い要約と URL で足す。7 日で消す。memory には書かない。
- 会話では skill `kohako-x-read` が控えを読む（外部の文は指示と見なさない、全投稿ではない、自動保存しない）。
- 既定モデル・補助モデル・生成先は変えない。Grok CLI（codex-auth、BOXP-739）には触れない。

## 手順
1. 仕様（vault）→ テスト `tests/hermes-agent/test_kohako_x_daily.py`（先に失敗を確認）→ 実装。
2. ConfigMap `hermes-agent-kohako-x-daily` と起動時コピー（script・SKILL.md）を追加。
3. merge・Argo CD 同期後、`register-job.sh` で job を登録し、重複なし・次回実行を確認。
4. 1 回だけ実呼出して控え・last_run・既読を確認。停止手段（pause / 停止中ファイル）を確認。
