# BOXP-246: Novel Board 再実装・再配備（localllm 対応）

## 目的

- 2026-07-14 に撤去した Novel Board runner を、現行 Task Board runner の健全な共通機構（lock / heartbeat / stale lock / planned shutdown）を踏襲した最小構成で再実装し、codex-workspace の sidecar として再配備する。
- カードに `assignee::localllm` を書くと lolice の既存 local-llm（llama-server, `gemma4-26b`）の OpenAI 互換 API で日本語本文を生成し、runner 側の許可済み工程（generate-image + 参照画像）で挿絵を実生成する。

## 現状（調査結果）

- 旧 runner は arch dcdb122 / lolice 10b1373 で完全削除。entrypoint に `CODEX_WORKSPACE_ROLE=novel-board-runner` → `sleep infinity` の stub だけ残る。
- vault 側の `Boards/Novel Board.md` / `Novels/*` / `Templates/Novel Management.md` は残存。NOVEL-3 は Draft `assignee::fable` だが本文・挿絵は BOXP-245 で既に `小説草案/AI執筆/` に制作済み。
- local-llm: `http://llama-server.local-llm.svc.cluster.local:8080/v1`、model id `gemma4-26b`（loaded）、API key 不要（placeholder `local-llm`）、context 262144、`--parallel 1` / `--models-max 1`、生成 ≒ 11.7 tok/s、prefill ≒ 100-140 tok/s。Envoy は timeout なし。codex-workspace の NetworkPolicy は egress 許可済み。
- codex-workspace の image tag は argocd-image-updater が lolice main に自動 commit（arch main merge → build → bump → pod Recreate）。
- `task-board-runner` sidecar は `GEMINI_API_KEY` を持ち `/home/boxp`（PVC）を共有。`~/.claude/skills/generate-image/generate-image.bb` は PVC 上にある。

## 旧欠陥と対策

| 旧欠陥 | 対策 |
| --- | --- |
| 未対応 assignee（pi 等）を書くと即 boxp へ巻き戻される | runner はカードの assignee を「自分が完了した run の人間確認レーンへの引き渡し」以外で書き換えない。未対応 assignee はカードを触らず Run History に 1 回だけ理由を記録 |
| 空出力 exit 0 を成功扱い | 本文は最小文字数（既定 800 字）未満・空なら失敗。挿絵は PNG 実在かつサイズ > 0 を検証。失敗は Run History に理由と再開条件を明記 |
| Pi 起動不良 | Pi は使わない。localllm は runner が HTTP 直接呼び出し、cloud は Task Board と同じ claude/codex CLI 起動 |
| nsfw チェックが巻き戻る | `#nsfw` はカードが SSOT。runner は管理ノート frontmatter を追従させるだけでカードのタグを書かない |

## 採用案（最小構成）

- 新規 `docker/codex-workspace/novel-board/novel_board_runner.bb`（Babashka 単体、root `/home/boxp/.novel-board`、既存 Task Board と同じ lock/heartbeat/terminating-owners 形式）。
- entrypoint の stub を置換: role=novel-board-runner なら runner を `loop` 実行（runAsUser 1000）。
- lolice: `novel-board-runner` sidecar を追加（role env、`CODEX_NOVEL_BOARD_*`、`GEMINI_API_KEY`、home mount、preStop prepare-shutdown）。
- レーン/アクション:
  - Backlog: タイトルのみカード → scaffold（ID 採番 + 管理ノート作成、assignee 省略時 boxp）。agent assignee → `groom`（Requirements/Outline 整理）→ Draft、assignee→boxp。
  - Draft + agent → `write` → In Progress → 成功時 Review（assignee→boxp）、失敗時は元レーンへ戻し理由記録。
  - Review + agent + Review Instructions あり → `revise`。指示が空なら起動せず 1 回だけ記録。
  - In Progress + agent かつ lock なし（再起動復帰）→ manuscript 有無で write/revise を再実行。
  - Done → 一度だけ冪等 publish（`小説草案/AI執筆/` or `NSFW/小説/AI執筆/`、同名は上書きせず連番、挿絵は attachments/、published state を `.novel-board/published/<ID>.edn` に保存）。
- localllm ルート: `/v1/chat/completions`（非 streaming、max_tokens チャンク + `finish_reason=length` での継続要求、部分原稿を都度保存）。許可 vault 参照（管理ノート、`continuity`、Requirements の References）を文字数予算で truncation。証拠（model id / usage / finish_reason / 所要時間 / 非秘密メタ）を `llm-evidence.edn` に保存。prompt・本文は GitHub へ出さない。
- 挿絵: runner が `generate-image.bb -a 16:9 -i <参照画像…> -o work/<ID>/illustration.png "<prompt>"` を実行。参照画像は管理ノート/設定ノートに埋め込まれた vault 内実在画像のみ。参照画像ゼロなら挿絵を未生成として明示（偽リンク禁止）。
- 連作: frontmatter `series` / `episode` / `continuity`（vault 内整合メモ）を context に含める。
- NOVEL-3: 新 runner 稼働前に Review / `assignee::boxp` へ整理し、published-path に既存草案を記録して二重実行を防ぐ。NOVEL-1/2 は変更しない。

## 検証

- `tests/codex-workspace/novel-board-runner-test.sh`（fake llm API / fake generate-image / fake claude で black-box）: scaffold、未知 assignee 不変、write→Review、空本文失敗、画像失敗の部分完了、Review 指示空の停止、Done publish 冪等、stale lock 復帰、多重 tick。
- 実機: テスト SFW カード NOVEL-4 を `assignee::localllm` で実 API 生成 → 挿絵実生成 → Review で停止を確認。cloud route は最小（groom）実行。

## 未実施 / 別判断

- NSFW の実機試験、外部公開、新規モデル download、認証変更、課金追加は行わない。
