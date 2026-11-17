# BOXP-599: コハコのナビログとプラグイン体験（実装計画）

採用設計: vault `Projects/kohako-home/netnavi-plugin-navilog-595.md`（BOXP-595、Owner 回答「ア」= 両方、ナビログ→プラグインの順）。
オペレート（途中で選ばせる操作）は入れない。普段の会話と既存の安全承認はそのまま。

## 実装前の確認結果（2026-11-17、本番 Hermes `ghcr.io/boxp/arch/hermes-agent:sha-3382c6a` を読み取りのみ）

| 確認項目 | 結果 |
|---|---|
| 実行ごとの道具制限 | `POST /v1/runs` は API キーの呼び出しで道具の指定を受け付けない（`hosted_room_dispatch` / `_room_execution_policy` は room grant が無いと 403）。ただし既設プラグイン `kohako-g2-context` の `pre_tool_call` が session_id の接頭辞で道具を**実行前に** block できる。現場の実行は `kohako-visit-` 接頭辞にし、Grafana の読み取り口だけを許す（段 2）。BFF の観測停止は二重の守り。 |
| Grafana 読み取り口 | `mcp_servers.grafana` enabled。grafana-mcp は `--disable-write`、`--enabled-tools search,datasource,prometheus,loki,dashboard,folder,alerting,annotations`。 |
| 家の会話の残り方 | Hermes session `kohako-web-<base36>`（通知返信 `kohako-web-reply-*`）として `state.db` に全発言が残る。 |
| Hermes 内部記録の保存期間 | `config.yaml` に `sessions:` 設定が無く `auto_prune` 既定 false。`state.db` の会話記録は**自動では消えない**（期限なし）。memory は既定 `memory_char_limit` 2200 / `user_char_limit` 1375 で期限なし。→ ナビログを消しても内部記録は消えない。保存期限の変更は本票の範囲外。 |
| 定期実行 | 10:00 朝報、13:00 PR 整理、平日 18:00 夕方、15 分・180 分の監視など。23:30 は空き。cron job は `enabled_toolsets` と `script`（stdout を prompt に注入、`{"wakeAgent": false}` で LLM を起動しない）を持てる。 |

## 段 1: ナビログ（この PR = lolice）

- `argoproj/hermes-agent/scripts/kohako-navilog/kohako_navilog_source.py`: cron の前処理。その日（JST）の `kohako-web-*` と `kohako-visit-*` の user/assistant 発言だけを `state.db` から**読み取り専用**で取り出す。Telegram・眼鏡・cron・webhook・道具の出力は読まない。通知返信は元通知の本文を省き本人の返信だけ。秘密らしき値・メール・電話番号は伏せる。1 件 700 字・現場報告 1200 字・合計 3 万字まで。
  - LLM を起動しない（何も書かない）: `ナビログ/停止中.md` がある / 本人が当日「今日は記録なし」と言った / 当日ファイルの frontmatter に `navilog: skip` / 材料なし。
  - 当日ファイルが既にあれば frontmatter `navilog_until` より後の発言だけを渡す（本人の編集・削除を書き戻さない）。
- init container が ConfigMap `hermes-agent-kohako-navilog-script` から `/opt/data/scripts/` へ置く。
- `kohako_navilog_prompt.md`: 夜のまとめの指示（4 種の行、8 行まで、載せない物、既存行を変えず末尾追記、最終応答 [SILENT]）。
- `register-job.sh`: 配備後に一度実行し、cron job「Kohako ナビログ（夜のまとめ・通知なし）」を登録。`30 23 * * *` Asia/Tokyo、`deliver=local`・`failure_deliver=local`（通知しない）、`enabled_toolsets=["file"]`（job 単位で道具をファイル操作だけに制限）。
- 家の会話への使い方の指示（過去の事はナビログを読んで答える、止める/再開、訂正）は arch の BFF instructions に足す（arch PR）。

## 段 2: プラグイン体験（arch + lolice、段 1 の後）

- lolice `kohako-g2-context`: `kohako-visit-` session は `mcp_grafana_*` の読み取り道具だけ許可、それ以外は実行前に block（委任・端末・ファイル・memory も不可）。
- arch BFF: 現場の実行（入口: 家の「見てきて」ボタン、会話で「プラグイン」「見てきて」だけの一言、警告通知の「見てきて」）。session `kohako-visit-<ts>`、上限 3 分・道具 20 回、許可外の道具が動いたら停止し「決まりの外に出たので戻しました」。戻りは 4 行（見た物／分かった事／分からなかった事／次に要る事）と見た時刻。現場の文は外の情報で本人の指示・承認にしない。
- arch web: 場面「現場」（CSS のみ、絵なし、行き先の札、道具名を平易な言葉に置換、動きを止める設定に従う、「確認」「つながらない」が優先）。

## 範囲外・しないこと

新しい権限・有料サービス・画像生成・Hermes 内部記録の保存期限変更・オペレート・Telegram/眼鏡の会話の記録。ナビログは vault の中だけに置き、票の Notes や PR に中身を写さない。

## 検証

- 単体: `python3 -B -m unittest tests/hermes-agent/test_kohako_navilog_source.py`
- 本番読み取り試行（書き込みなし）: 2026-11-17 に Pod 内で実行し 150 行（本人 52・通知返信 23・コハコ 75）を確認。本文は記録していない。
- 配備後: `register-job.sh --dry-run` → 登録 → `hermes cron run <id>` で当日分を 1 回書き、vault のファイルと通知が出ないことを確認。
