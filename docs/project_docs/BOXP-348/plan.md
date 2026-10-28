# BOXP-348: Task Board 通知への返答を Telegram / G2 で一回処理にする（Hermes 側 claim）

## 背景
- Task Board 通知は Telegram へ配信され、Owner の Telegram session に `[Webhook delivery: task-board]` として mirror される。Kohako G2 BFF（even-g2-lab, boxp/even-g2-lab#53）はこれを読み G2 のメッセージ窓に表示する。
- BFF（even-g2-lab ns）と Hermes（hermes-agent ns）は別 Pod で PVC を共有できないため、BFF 側の file claim では Telegram 側と原子的 claim を共有できない。
- 両経路の返答はどちらも Hermes の同一 process の `kohako-g2-context` plugin を通るので、claim の正本を plugin（Hermes PVC）に置く。

## 変更
- `pre_llm_call`: 返答対象の decision（`dec-<eventId>`）を特定する。
  - Telegram: Owner DM で、mirror された通知の直後の Owner 発言だけ（間に Owner 発言があれば対象外＝古い通知/履歴の「はい」を新しい判断へ接続しない）。
  - G2: BFF が入力 1 行目に付ける `[kohako-decision] reply decision=… operation=…`。
- `pre_tool_call`: 最初の副作用 tool 呼び出しで `/opt/data/kohako-decisions/claims/<decision>.json` を O_EXCL 作成。他経路が claim 済みなら副作用 tool を block（読み取り・雑談では claim しない）。保存失敗は fail closed。
- `transform_llm_output`: claim したターンの応答末尾に `[kohako-decision] done decision=…`（BFF が G2 表示で対応済みにする）。
- `post_llm_call`: claim を `done` に確定。結果不明（claimed のまま）は再実行しない。done は 7 日で GC。
- G2 返答の有効化（BFF の `KOHAKO_NOTIFICATION_CLAIM=hermes`）はこの plugin の配備・readback 後に別 PR で行う。

## 試験
- `python3 -B -m unittest argoproj/hermes-agent/plugins/kohako-g2-context/test_kohako_g2_context.py`

## 追記（plugin 1.3.2, codex-review 対応）

- `error` は副作用が起きていないと断定できない（変更後のエラー終了、外部操作成功後のタイムアウト等）ため、`failed`（再 claim 可）を廃止し、結果不明として `claimed` のまま保持する。どちらの経路からも再実行しない（読み取りと状況説明は可）。
- reclaim lock と `failed` 状態を削除。GC は `done` のみ（7 日）。
- 誤って commit されていた `__pycache__` を削除。unittest 29 件成功。

## 追記（plugin 1.3.3, codex-review 対応）

- Telegram の現在発言の除外は、履歴末尾の user 発言が現在の発言と一致（空白正規化後の包含）する時だけ行う。履歴に現在の発言が無く末尾が前回の Owner 発言なら、そこで探索を止め古い通知へ結び付けない。
- `date`・`git status/log/diff/show`・`gh pr view/checks` 等のシェル構文を含まない簡単な確認コマンドは claim しない（承認ゲートは従来どおり適用）。`--output`/`--pre` 等の書き込み・実行系オプションや連結・リダイレクト・置換を含むものは従来どおり副作用扱い。

## 追記（plugin 1.3.4, codex-review 対応）

- 「はい」が続く等、同じ文面の過去発言を現在の発言と取り違えないよう、現在の発言は本文一致に加えて Hermes がターン開始時に付ける `timestamp` で識別する。各 Owner Telegram ターンの現在発言 timestamp を Hermes PVC の `kohako-decisions/turns/<session hash>.json`（最新64件）に記録し、既に前ターンの現在発言として記録済み・timestamp なし・記録失敗の発言を末尾に持つ履歴で通知が見つかった場合は、検出エラーとして副作用toolを block（fail closed、読み取りは可、claim しない）。通知が見つからない通常ターンには影響しない。
- `[Webhook delivery:` で始まる通知ターン自体は返答として扱わない。
