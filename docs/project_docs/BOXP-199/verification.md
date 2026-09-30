# BOXP-199 確認結果・staging試験手順

## 実施済みの調査

2026-09-30 UTC、lolice worktree基点 `c3cea3b9fb505e89f206963829a5d17361e82ab0`。Pod/Deploymentのread-only照会は実行した。Secret値、runtime env、音声本文、ユーザー情報、Pod logsは取得していない。

| 確認対象 | 結果 |
| --- | --- |
| runtime Hermes | Running、1 replica、Recreate。image `ghcr.io/boxp/arch/hermes-agent:sha-cc3d5f1`、digest `sha256:3d4302c1c8ca03682dae0705c6202807481997880009460e0fdad8a1fd5365ea` |
| image build元 | arch commit `cc3d5f1cecdfe9af16742cc07b6adce9dbf639cc`、upstream base digest `sha256:94f90d3cb66c848e6d7465fb7ca11dc485f096700edc26f571fedab59f4274f7` |
| GitOps宣言との差 | worktreeのimage updater指定は `sha-ef1fe35`。runtime imageと異なる。今回変更・同期していない。現行Hermes source versionは未確認 |
| runtime設定 | Deploymentのenv名にDiscord関連設定は見つからない。ただしPVCの.env/config.yamlを確認できず、Discord未設定と断定しない |
| RBAC | `kubectl auth can-i create pods/exec -n hermes-agent` → no。Secret一覧もForbidden。権限を拡張しなかった |
| Discord inventory | application/bot、guild、専用非公開VC、利用者・権限・intents・pairingは未確認。作成・招待・joinなし |
| 公式実装 | 固定main revisionのDiscord pluginと音声受信・依存を確認。TTS通常経路の受信pause、INFO本文log、transcript投稿を確認。Desktopの機能をVCの対応として扱わない |
| 本番設定差分 | なし。文書のみを追加。Secret・NetworkPolicy・image・PVCへの適用なし |

独立調査を低コストagentへ委任した。clone/fetchは失敗したため公式公開ページの調査に切り替えた。親workerはGitHub contents APIで固定revisionのadapter/pyproject/docsを取得し、判断に重要なpause、本文log、依存を照合した。委任側のHTML表示行番号は実ファイルの行番号と一致しないため、そのまま根拠リンクに使用していない。

## 未完了の受入条件

| 受入条件 | 状況 |
| --- | --- |
| 現行版と公式headless対応の確認 | 公式revisionと経路は確認。runtime source version/依存は未確認 |
| Discord棚卸しまたは作成手順 | 棚卸しは取得不能。最小権限の作成・招待・pairing境界確認手順を設計に記載 |
| K8s設計PR | 設計文書をDraft PRへ提出。適用可能manifestはguardが未実装のため保留 |
| private VCでの実音声試験 | 未実行。遅延・品質・割り込み・障害回復の実測値なし |
| 承認・allowlist・rate limit guard | 必要な要件と否定テストを記載。追加guardは未実装、既存runtime承認設定も未確認 |
| staging結果・CI・rollbackを添付 | ローカル文書確認とrollback手順あり。staging実測なし、CIはPR作成後の状態を別途記録 |

## 再開に必要な前提

運用者による現行imageのHermes revision/依存確認、専用bot・非公開test guild/text channel/VCの確認、専用Secret参照の用意、非機密発話を行う試験者、審査済みTTS provider、staging専用egressの許可が必要。識別子やtokenをチケットへ貼らず、private設定に保存する。先にguardを別worktree/PRで実装して否定テストを通す。runtime調査権限が与えられても本番接続の許可にはならない。

## staging試験（すべて未実行）

guardのunit/integration試験 → 合成sentinelの非保存試験 → 専用VC試験の順に行う。sentinelは機密情報を含まない乱数文字列とし、本文をCIに出さず「残存0件」の成否だけを記録する。Sentinelをsession DB/ファイル/stdout/collector/Discord textで検出したら実音声へ進まない。

| ID | 操作 | 記録・合格条件 |
| --- | --- | --- |
| G01 | wrong guild/text/VC/user、DM、未知SSRC、pairing経由、bot入力、channel移動 | すべて拒否、STT/provider呼出し0、音声応答0、必要時leave |
| G02 | 音声起点sessionでexec/write/send/cron/MCP/tool call/承認を模擬 | dispatch0、承認処理0、bodyログ0。単に音声で「実行して」と言って応答を確認するだけでは不十分 |
| G03 | queue/発話時間/利用回数/provider timeoutを超過 | 上限内で破棄、無限retryなし、メモリ増大なし |
| P01 | 正常・拒否・例外・cancelにsentinelを含める | 音声本文/transcript/user IDの永続化・ログ・Discord投稿が0。音声providerの保存条件も確認 |
| V01 | test利用者がprivate VC参加、private textからjoin/status/leaveを3回 | join/leave各3/3、leave後受信0、他channel接続0 |
| V02 | 「こんにちは。短く挨拶してください」「一から三まで数えてください」各10回 | STT正答率と誤り分類、TTS冒頭欠落・聞き取り・音量を集計。認識本文そのものを保存しない |
| V03 | 音声応答中に「停止してください」と発話、10回 | 再生停止の成否と停止遅延。通常pause経路で失敗した場合は未達としてadapter改修へ戻る |
| V04 | stagingのみでprovider timeout、UDP遮断、Pod再起動、DAVE失敗を各1回 | 切断・queue破棄、秘密や本文logなし、権限再確認。自動joinなし、人の再join成功。gateway reconnectとVC rejoinは別集計 |
| V05 | 試験15分終了、idle 60秒、利用者退出/第三者参加 | 設計どおり停止・leave、常時接続なし |

STTは指定日本語定型文の正答率と無音誤起動数を評価し、TTSは明瞭性を5段階と冒頭/末尾欠落数で記録する。最低10試行のmedian/p95、最大値、失敗数を記録し、試行数の小ささを明記する。機密発話は行わない。

計測時計はPodのmonotonic clockを用い、`発話終了→STT完了`、`STT完了→LLM初token`、`LLM完了→TTS初音声`、`発話終了→利用者が聞く初応答`を分ける。最後の値はDiscord端末側の観測が必要。全体値には無音区切り約1.5秒とネットワークが入る。targetは初応答p95 5秒以内、割り込み停止p95 1秒以内を暫定案とし、実測結果と分けて記載する。PCMや会話録画を保存せず、試験者が時刻/成功判定だけ記録する。

## 費用

実行0回につき本workerによるvoice/STT/TTS provider利用料は0。stagingの費用実績は未測定。local STTと既存local LLMでもPod/GPU/電力・運用費がかかる。provider単価は未確定なので金額を推測しない。

cloud比較の見積式: `STT音声分 × 採用時点の単価 + TTS課金単位 × 単価 + LLM input/output token × 各単価 + staging計算資源費`。無音・retry・ack音声の課金も含め、providerの公式価格URLと確認日を実施時に記載する。Realtime経路を採用する場合は別の音声token体系として再見積りする。試験は15分以内・予算上限をprivate運用設定で事前指定し、usage集計のみを記録する。

## ローカル確認

`git diff --cached --check`、文書相対リンク確認、`kustomize build argoproj/hermes-agent`はすべてPASS。変更は文書3件のみ。`codex review --uncommitted`は明確な不具合の指摘なし（追加の公開ソース取得はHTTP429で制限された）。これらはvoice manifestや実接続の成功を意味しない。CI結果はPR作成後に確認する。
