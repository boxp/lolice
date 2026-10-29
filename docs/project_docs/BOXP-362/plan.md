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
修正前コードで新 fixture `test_compaction_summary` が同じ block を返すことを確認。

その他の候補（assistant 文中の引用・Webhook 包み・別件 user 境界・turn 未指定 fallback・`_pending` lifecycle・引用返信）は
既存実装/既存テストで誤接続しないことを確認（引用返信は `[Replying to: ...]` で始まり通知とは判定されず、user 境界になる）。

## 修正（kohako-g2-context 1.3.18）
1. compaction summary 行（user/assistant 役で `[CONTEXT COMPACTION` または `[PRIOR CONTEXT — for reference only` で始まる、または `_compressed_summary`）を検出しても走査は止めず「summary 越え」として記録する。
   summary が現在メッセージに merge された場合も同様。通知本文・tool 結果・assistant 文中のマーカー文字列は summary とみなさない。
2. Telegram の claim に、claim した Owner 発言の正規化 hash（`message`）と会話（session）key（`session`）を記録する（G2 claim には付けない）。
3. summary 越えで通知に到達した場合、claim が「Telegram 経由・done・期限内・**同じ会話（session）の別ターン**が claim・hash 記録あり・現在発言と異なる hash」のときだけ hard bind しない。
   根拠: 圧縮なしの履歴では通知直後の最初の Owner 発言だけが返答として bind され、2 通目以降は新規指示扱い（walk が前の Owner 発言で止まる）。
   同じ会話の別ターンが done claim を持つことは「最初の返答枠が消費済み」の永続的な証拠であり、圧縮前と同じ判定になる。
   この場合も gate は外すが、LLM context に「この通知は以前の Telegram 返答で既に対応済み。同じ操作の再依頼なら再実行せず対応済みと答える／別件なら通常対応」の警告を注入する（言い換え再返答への soft guard）。
   それ以外（未 claim・G2/他経路 claim・別会話の claim・session/hash 無しの旧形式 claim・同一ターン・claimed=対応中/結果不明・同一文の再送・読めない claim）は従来どおり bind → claim / block。
4. 時刻による新規判定は取りやめた（再送と新規指示を区別できないため）。
- 既知の制限: G2 で対応済みの通知の後、圧縮を挟んで Telegram で出した新規指示は block される（fail-closed）。
  旧版（1.3.15 以前）が作った claim（BOXP-360 の dec-... を含む）は session/message 記録が無い。これは「旧形式 claim」として次をすべて満たす場合だけ同会話の別ターンと判定し、soft 警告付きで hard bind を解除する（1.3.17）: Telegram・done・期限内・operationId が `tg-` で現在ターンと別・現在発言が turn anchor 済みで claim より新しい・この会話の turn 記録に claim 時刻の直前 600 秒以内（記録開始後）の Owner ターンがある。満たさない場合（対応中/結果不明・G2・圧縮なし・anchor 不可・turn 記録なし等）は従来どおり block（claim 削除はしない）。旧形式では発言 hash が無いため同文再送も soft 警告扱いになる（言い換え再返答と同じ水準）。
  1.3.18（codex-review 指摘: 並行ターン・別会話の旧 claim の誤帰属）: 新形式・旧形式とも、解除には現在発言が turn anchor 済みで、claim が done になった時刻より**後**であることを必須にする（claim 完了前に始まった並行ターンは block）。旧形式ではさらに、**他の会話の turn 記録**に claim done 時刻の直前 3600 秒以内の Owner ターンが一つも無いこと（全記録を読めること、記録が 64 件で満杯かつ最古が window 内なら押し出しの可能性があるので block）を必須にする。こうして claim を作れたのがこの会話だけと確定できる場合に限る。
  同会話で言い換えた再返答を圧縮越しに送った場合は hard block ではなく soft 警告になる（圧縮なし履歴の 2 通目と同じ扱い）。
- claim 削除・guard 無効化・別 tool 回避はしない。G2/Telegram 同時返答の一回処理と結果不明時の再送抑止は維持（既存テスト + 新テスト）。

## 検証
- `python3 test_kohako_g2_context.py` 56 件 OK（1.3.18）。新テスト 7 件（旧形式 claim の解除/維持 7 subcase を含む）のうち再現系は旧コードで失敗することを確認。
- 独立 review、CI、通常 merge、ArgoCD 配備、plugin version readback。

## 配備と Hermes 再起動
plugin は configMapGenerator 経由のため配備で hermes-agent pod が再作成される。
BOXP-360 の承認済み Hermes 再起動 1 回（lolice #943）と重複させないよう、配備タイミングは BOXP-360 担当と調整する。

## 関連
- Novel 挿絵不具合は BOXP-363 として正規登録・着手済み（重複起票しない）。
- 記録: Obsidian `Projects/kohako-decision-misbinding/owner-summary.md`
