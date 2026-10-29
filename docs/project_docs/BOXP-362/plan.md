# BOXP-362: 新規指示を過去の対応済み通知へ誤接続する共有decision制御の修復

## 観測
- 2026-10-29 06:39:19 UTC、Owner の Telegram ターンで非 read-only tool が
  `dec-BOXP-360-20261029T054657Z-...-review は Telegram で既に対応済み` で block された（Hermes WARNING ログのみ確認、会話本文は未収集）。
- この通知は 05:56 頃に Telegram の返答ターンが claim → done 済み。06:39 は Novel 挿絵不具合の新規依頼で、その返答ではない。
- G2 セッションでは非 terminal tool は先に `_NO_APPROVAL` で止まるため、この block は Telegram の履歴走査 (`_detect`) の結果。

## 原因（合成 fixture で再現）
`_detect` は履歴を逆走し、Owner の通常発言で止まり、それより後の通知を「返答先」とみなす。
Hermes の context compaction（`agent/context_compressor.py`）は中間ターンを summary 行に置き換える。
summary は直前 head が user なら `assistant` 役、tail 先頭とも衝突すれば tail の assistant 行に merge される。
保護された head の末尾がミラー通知（`[Webhook delivery: task-board]`, role=user）のとき、
走査は tail の assistant 行と summary を読み飛ばし、Owner の返答（summary に吸収済み）を越えて古い通知へ到達する。
→ 新規指示が対応済み decision に bind され、`lost=telegram` で副作用 tool がすべて block される。
修正前コードで新 fixture `test_compaction_summary_ends_the_walk` が同じ block を返すことを確認。

その他の候補（assistant 文中の引用・Webhook 包み・別件 user 境界・turn 未指定 fallback・`_pending` lifecycle・引用返信）は
既存実装/既存テストで誤接続しないことを確認（引用返信は `[Replying to: ...]` で始まり通知とは判定されず、user 境界になる）。

## 修正（kohako-g2-context 1.3.15）
1. compaction summary 行（`[CONTEXT COMPACTION`、`[PRIOR CONTEXT — for reference only`、`--- END OF CONTEXT SUMMARY` を含む、または `_compressed_summary`）で走査を終了。
   summary の向こうにある通知は直前の通知ではない（Owner 発言が吸収されている可能性）。
2. Telegram claim に会話 (`session` = sid の sha256 先頭 32 桁) と返答メッセージ時刻 (`stamp`) を記録。
   同じ会話で、その返答ターンが `done` になった後 (`at` < 今回 stamp) に送られたメッセージは返答ではないので bind しない
   （履歴から返答が消えても新規指示を古い claim で止めない）。
   - claim 中・結果不明 (`claimed`)、G2 claim、旧形式 claim（会話記録なし）、別会話は従来どおり block。
   - done 前に送られた二度押しは従来どおり block。
- claim 削除・guard 無効化・別 tool 回避はしない。G2/Telegram 同時返答の一回処理と結果不明時の再送抑止は維持（既存テスト + 新テスト）。

## 検証
- `python3 test_kohako_g2_context.py` 50 件 OK。新テスト 5 件のうち再現系は旧コードで失敗することを確認。
- 独立 review、CI、通常 merge、ArgoCD 配備、plugin version readback。

## 配備と Hermes 再起動
plugin は configMapGenerator 経由のため配備で hermes-agent pod が再作成される。
BOXP-360 の承認済み Hermes 再起動 1 回（lolice #943）と重複させないよう、配備タイミングは BOXP-360 担当と調整する。

## 関連
- Novel 挿絵不具合は BOXP-363 として正規登録・着手済み（重複起票しない）。
- 記録: Obsidian `Projects/kohako-decision-misbinding/owner-summary.md`
