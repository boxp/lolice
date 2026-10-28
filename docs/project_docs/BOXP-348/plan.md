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
