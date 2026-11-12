# BOXP-503 実施計画: kohako-home の Web 向けツール制限を解除する

## 原因（2026-11-11 時点の稼働 Pod で確認）
1. Hermes の `platform_toolsets.api_server` はすべての API session で共通。値は G2 向けの `[no_mcp, file, session_search, search, skills, terminal]`。
2. `kohako-g2-context` の pre_tool_call gate は、`kohako-g2-*` 以外の api_server session（kohako-home の `kohako-web-*` を含む）のツールをすべて block する。
3. 2 つの BFF（even-g2-main と kohako-studio）は `/v1/toolsets` が `KOHAKO_HERMES_TOOLSETS` と完全に一致しない限り送信しない（interlock）。

## 方針
- `api_server` toolsets を Telegram 相当に広げる。`no_mcp` を外すので MCP の grafana / langfuse も対象になる。
- session ごとの制御は gate だけで行う。
  - `kohako-web-*`: Owner のテキストターンに限り許可する（gate は介入しない）。Hermes 本来の危険コマンド・書き込みの承認（Telegram と同じ）は残り、BFF の once/deny UI に出る。
  - `kohako-web-*` のなでる/手タップのターン（BFF の固定テンプレート）、turn id が空または不明なターン、multimodal 入力: 全ツールを block する。
  - 上記の許可されたテキストターンから起動した delegate_task の子 session: 許可する。
  - `kohako-g2-*`: 従来の allowlist と承認のまま変えない。新しく増えたツールは `_NO_APPROVAL` で block される。
  - `kohako-g2-cand-*` / `kohako-verify-*` / その他の api_server session: 従来どおり全 block。
- `kohako-web-` の prefix は、kohako-studio BFF が Cloudflare Access JWT の Owner email を server 側で検証した場合だけ付ける。verifier（codex-workspace SA の TokenReview）には `kohako-verify-` を付ける。browser 側から session id は指定できない。
- 両 BFF の `KOHAKO_HERMES_TOOLSETS` を新しい `/v1/toolsets` の報告値に揃える。

## Telegram との対応表
| Telegram の toolset | kohako-home | 備考 |
|---|---|---|
| file / terminal / skills / session_search / web / memory / todo / browser / code_execution / delegation / cronjob / connections / vision | 対応 | Hermes の標準承認を維持する |
| MCP grafana / langfuse | 対応 | 既存の read-only MCP。新しい account や scope は追加しない |
| clarify / tts / computer_use | 未対応 | API server では表示できない（Hermes の hermes-api-server bundle も除外している） |
| image_gen | 未対応 | 新たな課金や画像生成を避けるため除外 |

## リスク
- G2 の LLM にも、増えたツールの schema が渡る（Telegram と同じ量）。gate が block するので実行はされないが、遅延が増えないか配備後に確認する。
- Web から cronjob / delegation を Telegram と同じ権限で使える。

## 検証
1. plugin の unit test。bootstrap の構文と、旧値 3 種からの移行シミュレーション。
2. merge 後に Argo の同期、Pod の config、`/v1/toolsets` を読み戻す。両 BFF が `toolsets_open` を出さないこと（G2 と home の非退行）も確認する。
3. 実 Hermes:
   - Owner のテキストターンで、read / search / skill / Task Board 照会 / MCP read-only を実際に呼び出す。
   - なでるターンではツールが動かないこと。
   - verifier session が block されること。
   - 承認の once / deny / 期限切れ / replay / 停止。副作用のない専用コマンドで確かめる。
4. 実ブラウザ:
   - verifier として home を開き、ツールが拒否されることを確かめる。
   - Owner の操作は本人の認証が必要なので、agent が再現できない部分は「未確認」と明記する。

## Rollback
この PR を revert する。旧 bootstrap は新しいリストを管理値として認識せず、WARNING を出して残す。そのため revert 時には、`PREVIOUS_*` に新しいリストを加えた bootstrap を併せて適用する（あるいは PVC の `/opt/data/config.yaml` の api_server を旧値に戻して Pod を再起動する）。そのうえで `/v1/toolsets` が旧 4 種に戻ったことを確認する。
