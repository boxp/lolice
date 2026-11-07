# boxp-langfuse-observer: 観測契約と保護境界（BOXP-428）

Owner が確定した契約（`Projects/langfuse-observability/request.md` 冒頭、2026-11-06）: **通常の依頼・応答・tool 入出力・委譲本文を可視化**し、**明確な秘密は発生源/保存送信前に除外**し、**無印平文や未知変換の完全検出不能は残差として許容**する。Langfuse は lolice 内 self-host、Owner 限定 Access、短期 retention。

## 1. 保証するもの（レビューの対象）

| 境界 | 実装 | 試験 |
|---|---|---|
| **K0 既知機密源の非収集** | `_SENSITIVE_PATH`（認証ファイル: `.env` `.ssh` `.aws` `kubeconfig` `.kube/config` `auth.json` `.netrc` `.git-credentials` `id_*` `.gnupg` `secrets/` token ファイル(拡張子付き/パス要素) `*.key *.pem *.p8 *.p12 *.pfx *.jks *.keystore *.ppk` `.npmrc` `.pypirc` `.pgpass` `.my.cnf` `.docker/config.json` `.boto` `.s3cfg` `.vault-token` `.terraformrc` `.htpasswd` `gh hosts.yml` `gcloud/` `.azure/` `cargo/gem credentials`）、`_SENSITIVE_COMMAND`（`env` 単独/オプション付き、`printenv` `export -p` `declare -p` `environ` `process.env` `getenv` `%ENV`、`kubectl/oc … secret`、`aws ssm get-parameter / secretsmanager / configure export-credentials / sts get-session-token / assume-role / ecr get-login-password / iam create-access-key`、`gcloud auth print-*-token / application-default print-access-token / secrets versions access`、`az account get-access-token / keyvault secret show`、`kubectl create token`、`oc whoami -t`、`vault read/kv get/token/login`、`op read/item get`、`docker-credential-*`、`helm get`、`gh auth token`、`git credential`）、`_is_env_dump`（出力に `PATH=` と `HOME=`）。該当する呼出しは引数も結果も送らず（`withheld_known_source` / `withheld_env_dump`）、その session と委譲の親・子の以後の本文も送らない。**表は固定リストでここで閉じる。** | unit `test_sensitive_tool_output_is_withheld` `test_known_source_table_additions_are_withheld` 他、Chainsaw `fire_flagged_secret_read` `fire_env_dump` `fire_known_source_table` |
| **K1 既知形式のマスク** | `redact()`: 環境変数の実値（全エンコード形）、`<password>` 等の XML 要素、`password: …`/`api_key=…` の代入（配列/マップ/YAML ブロック含む）、秘密語を含む行とその前後行、`_PATTERNS`（private key, Authorization/Cookie ヘッダ, URL 資格情報, JWT, provider key, AWS, Google, Telegram, Bearer, env entry, `--password x`, `curl -u`/`mysql -p`/`sshpass -p`, data URI, 高エントロピー列）。全 field に適用し、送信直前の JSON を `_unsafe` で再走査してヒットした span は破棄。 | unit `test_redacts_every_synthetic_secret` `test_secrets_never_reach_payload_from_any_field` 他 |
| **K1 露出後の保留** | tool 結果に K1 形式（秘密語だけの言及・エントロピー・data URI を除く）が実際に当たった場合、その本文はマスクして送り（`masked_known_format`）、以後の session 本文を保留（`withheld_after_known_format_in_tool_output`）。 | unit `test_known_format_in_a_tool_result_masks_it_and_withholds_later_bodies`、Chainsaw `fire_known_format_in_output` |
| **走査不能は fail-closed** | 1 MiB 超の tool 結果は走査せず本文と以後を保留（`withheld_unscanned_output`）。redact の例外は field を `[dropped: redaction error]` にし露出扱い。 | unit `test_oversized_tool_result_is_withheld_unscanned` |
| **系譜** | 保留理由は `subagent_start` 後の子に開始時点で、親には子の報告時点で伝わる。 | unit `test_taint_reaches_parent_and_child` |
| **未観測履歴** | 再起動後の履歴は tool 呼出し引数(K0)と tool 結果(env dump / K1)を同じ基準で走査する（メッセージごとに 1 回）。 | unit `test_evidence_in_unobserved_history_withholds_without_its_hook` |
| **queue 前マスク** | `on_session_end` で build→redact→最終ガードまで済ませ、queue には JSON batch 文字列だけを積む。ディスクには何も書かない。 | unit `test_queue_only_holds_masked_batches` |
| **鍵なし期間** | 送信はしないが保留状態は追跡する。 | unit `test_hooks_idle_without_key_and_start_when_key_appears` |

## 2. 保証しないもの（Owner 承認済みの残差。レビューで P0 にしない）

- 秘密語も既知形式も無い**無印の平文値**（例: 通常名ファイルの中身、`cat /etc/app/config.yaml` の値）と、モデルがそれを**裸で再掲**したもの。
- 既知形式に当たった値を、モデルが**別の位置・言い換え・未知の変換**（base32/rot13/gzip/二重符号化 等）で再掲したもの。値の pin や変換列挙は行わない。
- 同じ機密コマンド/ファイルの**恣意的なシェル表記**（引用の分割、エスケープ、グロブ、変数経由、未知の別名 CLI）で K0 表を外れるもの。
- Owner の依頼文や system prompt に形式も秘密語も無く書かれた値。
- 秘密語を含む行とその前後行は内容に関係なく伏せるため、無害な語も消える（過剰マスク）。tool 名だけ・単語の言及だけ・未観測履歴だけでは**保留しない**。

補助の守り: self-host、Owner 限定 Access、14 日 retention、MinIO 1 日、spool 無し。

## 3. 陽性可視性（空にすれば秘密ゼロ、という退行を不合格にする）

unit `test_a_harmless_turn_is_not_all_withheld` `test_reply_after_an_ordinary_tool_is_sent` `test_reply_after_a_child_used_a_tool_is_sent` `test_tool_bodies_are_sent_through_the_mask`、Chainsaw `fire_harmless_tool` `fire_harmless_lineage`（terminal/read_file/未知 tool の後の応答・次ターン・委譲子・親の要約が sink に原文で届き、`body_policy=sent` が 10 件以上）。各観測の `metadata.body_policy` と trace の `coverage.withheld_reason` で producer 別・理由別の比率を Langfuse 上で読める。
