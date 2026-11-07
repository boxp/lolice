# BOXP-428 本文可視化の最小是正（Fable 引継ぎ、2026-11-07）

## 背景
- 配備中の Hermes observer / Task Board exporter は「delegate/todo/clarify/web_search 以外の tool を 1 回使うと系譜全体の本文を無期限に伏せる」設計で、Owner 報告「ほぼ全 trace が omit」はその結果（BOXP-458 診断）。
- 修正 PR lolice#1091 / arch#14503 は「秘密語の近傍の値を pin して以後マスク」方式に膨らみ、値の位置・形・変換の列挙が閉じず review gate が 68 run 分収束しなかった。

## 方針（BOXP-458 推奨案 3.1 を採用）
1. main を基礎に新 branch で最小差分。tool 名で伏せる条件を外し、通常 tool の入出力を既存の既知形式マスク経由で送る。
2. 以後保留は K0（既知機密源・env dump）と、tool 結果への K1 実ヒットだけ。未観測履歴は同じ基準で走査し、tool 名や未観測の事実では保留しない。
3. 値 pin / 変換照合は保証に含めず取り込まない。K0 表に 9 系統を足して閉じる。
4. 各観測に `body_policy`、trace に `coverage.withheld_reason` を出し、理由別比率を可視化。
5. queue 投入前にマスク（on_session_end で build→redact→最終ガード）。
6. THREAT_MODEL.md を「保証する / 保証しない」の 2 節で diff に含める。

## 試験
- unit: 陽性（terminal/read_file/未知 tool 後の応答・次ターン・委譲子）、退行検出（harmless turn で sent≥3 かつ withheld 0）、K1 ヒット→マスク+以後保留、K0 表追加の陽性/陰性、queue にマスク済みのみ、鍵なし期間の状態追跡、1 MiB 超 fail-closed。
- Red: 新テストを main の `__init__.py` に当て 106 failures / 8 errors（陽性・K0 表・履歴規則）。Green: 66 tests OK。
- Chainsaw `tests/chainsaw/hermes-agent`: driver に陽性 4 系統と K1/K0 陰性を追加し、既存陰性は維持。ローカル driver+sink 模擬で main は陽性 12 marker 欠落・sent 0（Red）、新実装は欠落 0・漏えい 0・sent 256（Green）。実 CI の run ID は PR/Notes に記録。

## 停止ルール
- gate の P0 が THREAT_MODEL.md 2 節（保証しないもの）に当たる場合は直さず、Notes に P0 本文と該当節を記録して owner-decision で停止する。予算 reset/grant/迂回はしない。
- 保証内（K0 表の漏れ、K1 形式の退行、queue 前マスク、系譜）の P0/P1 は直す。

## 後続
- arch: exporter に同じ契約（子 Codex へ委譲、親が検証）→ PR → merge → image → lolice の task-board fixture 固定更新。
- merge → ArgoCD 同期（Hermes は ConfigMap 更新で Pod 再作成、承認済みの通常再起動）→ 実小依頼 1 件で UI/保存先の陽性と合成秘密の陰性 → 入口 owner-summary.md 更新。
