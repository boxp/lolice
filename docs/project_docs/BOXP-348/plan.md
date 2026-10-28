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

## 追記（plugin 1.3.5, codex-review 対応）

- Telegram 側の通知認識を BFF（`apps/kohako-bff/src/notifications.ts` の `extractNotices`）と揃える: 先頭空白を許す `[Webhook delivery: task-board]`、`【Task Board 通知】` とチケット番号の間の空白（全角含む）、`[A-Z]+-数字` のチケット、語境界付きの最後の `event=`、`[SILENT]` 除外。G2 で返答できる通知は必ず Telegram 側でも claim 対象になり、空白の有無で片側だけ claim されない。

## 追記（plugin 1.3.6, codex-review 対応）

- GC と claim の競合: 期限切れ `done` を読んでから unlink するまでの間に別経路が新しい claim を作ると GC がそれを消し、両経路が claim できた。store 直下の `claims.lock` を `fcntl.flock` で排他し、GC・O_EXCL claim・確定（done/解放）を同じ lock 下で直列化（スレッド・別 process 共通）。GC の unlink 中に別経路が claim する順序を再現する並行 fixture を追加（lock なしでは失敗することを確認）。

## 追記（plugin 1.3.7, codex-review 対応）

- 未記録の過去発言の誤認: 履歴に現在発言がなく、末尾が未記録の同じ発言（plugin 導入前や記録 64 件から外れたもの）だと現在発言と誤認し古い通知へ接続し得た。現在発言と認めるのは timestamp が現在時刻から -60 秒〜600 秒以内、かつ記録済みの全ターンより新しい時だけにし、それ以外は曖昧として通知が見つかれば副作用 tool を block（fail closed）。
- 期限切れ `done` が残り続ける: `_existing` が `done` の期限を見ず、同じ通知への返答が `_claim`（GC）に届かないため 7 日を過ぎても block され続けた。期限切れ `done` は未 claim 扱いにし、次の claim の GC（store lock 下）で消してから O_EXCL claim する。

## 追記（plugin 1.3.8, codex-review 対応）

- 同一 session の並行ターン: pending を session 単位で持ち、次ターンの `pre_llm_call` が先行ターンの判定を消して、先行ターンの副作用 tool が claim なしで通り得た。Hermes は `pre_llm_call`/`pre_tool_call`/`post_tool_call`/`transform_llm_output`/`post_llm_call` の全てに `turn_id` を渡す（稼働 pod の turn_context.py・turn_finalizer.py・model_tools.py で確認）ので、pending を (session id, turn id) 単位で持つ。turn id の無い hook は、その session に開いている判定ターンが 1 つだけの時だけ対応付け、2 つ以上なら副作用 tool を block（fail closed）し、確定もしない（claim は結果不明のまま残る）。
- 未記録の直近の同文発言: 鮮度と記録の欠如だけでは現在発言と言えない（導入直前の「はい」）。ターン記録に開始時刻 `since` を持たせ、Owner Telegram の各ターンの現在発言をそれ以降すべて記録する。現在発言と認めるのは `since` より後・記録済み全ターンより新しい・新鮮、の全てを満たす時だけ。記録を始めた最初のターン（導入直後）は返答扱いせず、通知が見つかれば副作用 tool を block する。

## 追記（plugin 1.3.9, codex-review 対応）

- 判定上限での実行中ターン削除: pending が 256 件を超えると最古の判定を無条件に削除し、そのターンが後で副作用 tool を呼ぶと判定なしとして gate を通り、もう一方の経路の claim 済みでも重複実行し得た。開いているターンの判定は追い出さず、`post_llm_call` で外す（`post_llm_call` に届かなかったターンだけ 24 時間で期限切れ）。期限切れを除いても上限なら、新しいターンの判定を fail closed（claim せず副作用 tool を block）にする。

## 追記（plugin 1.3.10, codex-review 対応）

- pre_llm_call 時点の「他経路で対応済み」判定はスナップショットに過ぎないため、副作用 tool ごとに原子的 claim を再試行する。他経路の操作がすべて拒否され claim が解放された後は、もう一方の経路で対応できる（claim 済みなら従来どおり block）。判定不能（error）は引き続き fail closed。

## 追記（plugin 1.3.11, codex-review 対応）

- 同一ターン内の再試行: 副作用 tool が error（または post_tool_call 未着）で結果不明になった後も、同じターンの後続の副作用 tool は claim 済みとして通っていた。外部操作が成功後にタイムアウトした場合、モデルの再試行で重複実行し得るため、結果不明が記録されたターンでは以降の副作用 tool を block する（claim は結果不明のまま残り、両経路とも再実行しない）。

## 追記（plugin 1.3.12, codex-review 対応）

- 同一ターン内の並行実行: 結果不明は post_tool_call のエラー報告後にしか記録されないため、先行の副作用 tool が実行中（結果未報告）の間に呼ばれた後続の副作用 tool は通っていた。先行操作が成功後にタイムアウトすると重複実行し得るため、実行中の操作があるターンでは後続の副作用 tool を block し、先行操作の結果が返ってから続ける（読み取りは可）。

## 追記（plugin 1.3.13, codex-review 対応）

- マルチモーダルの Owner 発言: Telegram の履歴探索は文字列でない content を role に関係なく読み飛ばしていたため、通知の後に Owner が画像付きなどの発言をし、その後に別の指示を出すと、その指示を古い通知への返答として claim していた。文字列でない user 発言も探索の境界とし、古い通知へ接続しない（文字列でない assistant 発言は従来どおり読み飛ばす）。
