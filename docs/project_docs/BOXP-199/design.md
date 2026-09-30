# Discord Voice / Kubernetes設計（未適用、No-Go）

## 判断

公式gatewayのDiscord platform pluginを使うheadless経路は存在する。ただし現行配備のHermes revision・音声依存を確認できず、専用test channelも未確認。録音・transcriptの非保存と承認境界を満たすguardを実装・検証するまで接続しない。このPRは設計のみで、Argo CDへの追加、Secret投入、接続操作は含まない。

## 根拠とversion

2026-09-30 UTCの調査対象は公式mainの `57a22675ef9f7761111feba9d24e5c366db3134b`。検証候補をこのrevisionに固定する。最新release APIは [v2026.9.24](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.24) を返したが、releaseの内容とmainが同一であるとは扱わない。最古の対応versionは未確定。最新タグだけでvoice対応を保証せず、必要機能を含むcommitとコンテナdigestの組で採用する。

| 項目 | 根拠 |
| --- | --- |
| gatewayのDiscord plugin | [plugin定義](https://github.com/NousResearch/hermes-agent/blob/57a22675ef9f7761111feba9d24e5c366db3134b/plugins/platforms/discord/plugin.yaml)、[adapter](https://github.com/NousResearch/hermes-agent/blob/57a22675ef9f7761111feba9d24e5c366db3134b/plugins/platforms/discord/adapter.py) |
| VCのjoin/leaveと音声ループ | [公式Voice ModeのDiscord節](https://github.com/NousResearch/hermes-agent/blob/57a22675ef9f7761111feba9d24e5c366db3134b/website/docs/user-guide/features/voice-mode.md#discord-voice-channels) |
| Discord設定・認証・intents | [公式Discordガイド](https://github.com/NousResearch/hermes-agent/blob/57a22675ef9f7761111feba9d24e5c366db3134b/website/docs/user-guide/messaging/discord.md) |
| Python依存 | [pyproject.toml](https://github.com/NousResearch/hermes-agent/blob/57a22675ef9f7761111feba9d24e5c366db3134b/pyproject.toml): Discord extraは `discord.py[voice]==2.7.1`、voice extraはlocal STT等。uvのPyNaCl overrideも含めlockを尊重する |
| Discord transport | [Discord公式Voice Connections](https://github.com/discord/discord-api-docs/blob/main/developers/topics/voice-connections.mdx)、[DAVE仕様](https://github.com/discord/dave-protocol/blob/main/protocol.md): voice gatewayが通知するUDP endpointとE2EE対応が必要 |
| 既存imageの由来 | [arch Dockerfile](https://github.com/boxp/arch/blob/cc3d5f1cecdfe9af16742cc07b6adce9dbf639cc/docker/hermes-agent/Dockerfile): upstream base digestを固定。ただしHermesソースrevisionはこれだけでは特定できない |

Discord側の接続はvoice WebSocketとUDPで処理されるため、Podにマイク・スピーカー・Desktop GUIは不要。CLIのローカル音声入力やDesktopのfull-duplex Voice ModeをVCの機能と混同しない。採用経路は `Discord PCM → STT → Hermes turn → TTS → Discord Opus`。Realtimeモデル直結ではない。CLI/Desktop向けbarge-inの説明をVCへそのまま適用せず、adapterと実機で別途確認する。

コンテナの必須確認は `discord` plugin有効、discord.py/PyNaCl/davey、libopus、ffmpeg/ffprobe、local STT利用時はfaster-whisperとモデル。DAVEの送受信成功を確認し、非暗号化へのfallbackは認めない。必要なextraをbuild時にインストールし、起動時pip installやlatest追従は避ける。公式のvoice doctorスクリプトは出力に識別子が含まれる可能性があるので、そのままCI/Notesへ貼らない。

## 現行配備との分離

`argoproj/hermes-agent` は1 replica/Recreate、PVCに設定・認証・Obsidianを保持し、cloudflaredとObsidian同期のsidecarを持つ。bootstrap configは初回だけPVCへコピーされるので、ConfigMap変更だけでruntime設定が切り替わるとは扱わない。

stagingは別namespace `hermes-discord-voice-test`、別bot、別config、専用の一時homeを持つDeploymentとする。既存PVC、Vault、OAuth、Google token、cron、MCP、cloudflared、同期sidecar、Kubernetes ServiceAccount権限は継承しない。replicas 1/Recreateで重複voice sessionを避ける。Pod停止時に切断し、新Podは自動joinせず、再度人が明示的にjoinする。可用性より安全な停止を優先する。試験は最大15分、idle auto-leaveは60秒を候補とする。

root filesystemは可能ならreadOnly、非root、capabilities ALL drop、automountServiceAccountToken false。home/tmp/PCM/TTS/cacheはsizeLimit付きemptyDir（機密性確認時はMemory）に限定し、モデルcacheだけ別のreadonly volumeへ置く。モデルdownloadはbuildまたは事前準備とし、実行中の任意外部downloadを避ける。一時領域でもlog collectorに転送されるstdout、Discord投稿、provider側保存は防げないため、下記guardが必要。

gatewayのprocess probeと音声の健全性を分ける。音声未接続をliveness failureにしない。startupProbeにはモデルload猶予を与え、provider失敗時は無限再起動しない。CPU/memoryはlocal STTの実測から決める（初期候補requests 1 CPU/2Gi、limits 2 CPU/4Gi）。OOM時のfail-closedとqueue破棄を確認する。

## Secretと設定差分の設計

適用可能manifestはguard実装後の別変更で作る。今回の本番manifest差分はゼロ。候補差分は専用namespace/Deployment、専用Secret参照、専用NetworkPolicy、guard付きimage、設定である。Argo CD rootやimage updaterには登録しない。

credentialは既存External Secretsの管理方式に合わせたstaging専用Secretから `valueFrom.secretKeyRef` で渡す。`envFrom`で本番credentialをまとめて渡さない。キー名は `DISCORD_BOT_TOKEN` とprovider専用キーのみ。optional falseで未設定時は起動失敗させる。token、個人のDiscord ID、guild/channel IDはGit/Notesに残さず、制限値もprivate設定に保持する。Secretの作成・読取権限は運用者に限定し、kubectlのYAML dumpやenvの出力をしない。

公式設定の候補:

- `DISCORD_ALLOWED_USERS`: numeric IDで試験者だけ。role allowlistは使わない。
- `DISCORD_ALLOWED_CHANNELS`: 専用command text channelだけ。VCのjoin先は別guardでチェックする。
- `DISCORD_ALLOW_ALL_USERS=false`, `GATEWAY_ALLOW_ALL_USERS=false`, `DISCORD_REQUIRE_MENTION=true`, `DISCORD_HISTORY_BACKFILL=false`。
- `discord.auto_thread: false`, `discord.voice_channel_inactivity_timeout_seconds: 60`。`DISCORD_HOME_CHANNEL`とproactive sendを設定しない。
- 初期STTは `stt.provider: local`, `stt.local.language: ja`, モデル `base`。日本語品質が不足したらモデル増強または審査済みcloud STTを比較する。
- 初期TTSは審査済みローカル日本語providerを用意できるまで保留。Edgeは無料でも外部送信である。OpenAI/ElevenLabs等を使う場合は非機密文限定・provider保存条件・費用上限を事前確認する。

これらの環境変数だけでguild/VC/利用者のAND条件や音声承認の禁止を保証できない。独自の環境変数名を公式対応のように記載・適用しない。

## 追加guardの実装要件（未実装）

音声内容をプロンプトで禁止するだけでは保護にならない。adapter入口とtool dispatchの両方で強制する。

| 境界 | 必須挙動・否定テスト |
| --- | --- |
| join | 設定したguild AND command channel AND voice channel AND numeric user一致を確認。DM、別guild、公開VC、channel移動、未設定値は拒否。join時の権限を再取得する |
| receive | SSRCを確実なDiscord user IDに解決し、許可者だけSTTへ渡す。未知SSRC/bot/非許可者/異常packetは破棄。新規参加者やbotの移動で安全条件が崩れたら即leave |
| slash/DM/pairing | `/voice`以外の管理操作、`/approve`、設定変更、pairingによる拡張を拒否。既存pairingデータを持ち込まない。voice経由の「はい」を承認transportへ渡さない |
| agent/tool | 音声sessionに空のtool allowlistを強制し、terminal/exec/write、ネットワークtool、send_message、cron、delegation、skills/MCP、記憶更新を遮断する。安全な雑談・音声返答だけ許可。モデルからtool callが返ってもdispatch前に拒否する |
| 誤認識/限流 | 利用者ごと6 turn/分、1 guild 1 active turn、最大発話15秒、queue 1、試験15分の上限を候補とする。超過やSTT/TTS timeoutではqueue/PCMを破棄し、自動retry送信をしない |
| privacy | adapterのtranscript投稿・本文loggerを無効化。[音声入力処理](https://github.com/NousResearch/hermes-agent/blob/57a22675ef9f7761111feba9d24e5c366db3134b/plugins/platforms/discord/adapter.py#L3897)はINFOでuser IDとtranscript先頭100文字を出力するため、標準log levelだけで解決しない。session DB/history/memory/trajectory/LLM debugにも音声本文を永続化しない。成功時だけでなく例外時・拒否時も本文を出さない。採用revisionを実際に追跡して全経路を確認する |
| 障害・承認 | 接続timeout/UDP喪失/DAVE失敗/認証失敗/provider failureは停止またはleave。復旧後はallowlist/権限を再確認し、人のjoinまで待つ。音声で本番操作や外部送信を自動承認しない |

[adapterの通常再生経路](https://github.com/NousResearch/hermes-agent/blob/57a22675ef9f7761111feba9d24e5c366db3134b/plugins/platforms/discord/adapter.py#L3691)はTTS中に受信をpauseする。mixer経路は異なるが、barge-in成功の実測はない。割り込みを満たせない場合は受入条件未達とし、Desktopの設定で解決したことにしない。必要ならDiscord専用のecho suppressionと音声cancelを実装してから再試験する。

## egress / DNS

既存Calico policyはDNS TCP/UDP 53、local LLM TCP8080、外向けTCP22/80/443/7844、UDP2408を許可する。UDP2408はDiscord voiceの一般的な許可ではない。

stagingではdefault deny ingress/egress、kube-dns TCP/UDP53、Discord REST/gateway/voice WSSのTCP443、local LLMのTCP8080のみを基本とする。UDPはvoice Ready応答のIP/portへ必要となる。FQDNだけのpolicyや固定50000–65535推測では保証できない。運用者が事前の専用ネットワーク試験で通知endpoint/NAT到達性を確認し、private CIDR・metadata endpointを除外したvoice宛UDPを専用Podに限定する。動的endpointに追従するegress制御を選べない場合はNo-Goを維持する。汎用UDP allowを既存Hermesへ追加しない。hostNetwork/NodePort/公開ingressは不要。

provider通信を加える場合も専用egressだけにする。クラスタはIPv6 egress問題があるため既存 `gai.conf` のIPv4優先方針を確認する。Cloudflare tunnelやWARPだけでDiscord UDPの疎通を保証しない。

## Discordの棚卸し・未作成時の手順

現時点でbot/application、guild、専用VC、権限、intents、invite/pairing、join可否はすべて未確認。既存ark-discord-botの存在はHermesのbotやvoice権限が存在する証拠ではない。

運用者がDeveloper Portalと専用test guildで以下を実施し、こちらには識別子そのものを共有せず可否を報告する。

1. applicationのownerとbotを確認する。既存botを流用せず、試験専用applicationを候補とする。不要なpublic installを有効化しない。tokenは秘密管理へ直送する。
2. 専用test guild、private command text channel、private voice channelを作成。`@everyone`にはView Channel/Connectをdenyし、botと試験者だけ許可する。他roleのoverwriteも確認する。
3. OAuth2 scopesは `bot` と `applications.commands`。permissionはView Channel、Send Messages、Connect、Speakを基本とし、必要ならUse Voice Activityを追加する。Administrator、Manage Guild/Channels/Roles、Move/Mute Members、Webhook、Create Invite、public threads、Attach Files、Read Historyは与えない。自動thread/backfill等を無効化し、この最小権限で動くか試験する。参考は[Discord公式permissions](https://github.com/discord/discord-api-docs/blob/main/developers/topics/permissions.mdx)。
4. Guilds/Voice States等の通常intentsとMessage Contentを確認。numeric user IDのみ・roleなしならMembersは要求しない構成を候補とし、Presenceは不要。公式voiceガイドの「全部ON」の表とDiscordガイドの条件付き説明が異なるため、採用adapterの実際のIdentify payloadを確認する。
5. 招待URLはPortalで生成し、運用者が対象test guildを確認して招待する。本番guildへの招待はしない。allowlistと追加guardを別途設定し、DM/pairingが勝手に許可範囲を広げないことを否定テストする。
6. botのchannel権限と利用者本人のvoice参加を確認し、private command channelで `/voice join` を人が実行。`/voice leave` とDiscord側disconnect、Pod停止の三つの停止手段を確認する。

## 観測とrollback

記録するのはtest case ID、回数、段階別所要時間、成功/失敗コード、再接続数、CPU/memory、provider usage/costの集計だけ。利用者名/ID、guild/channel ID、音声、transcript、token、provider exception本文は記録しない。Loki等へ本文が流れないことを合成sentinelで先に確認し、残存した場合は音声検証を中止する。

異常通知は接続失敗回数・provider timeout・OOM・上限到達・privacy guard failureだけを送る。voice sessionのidle離脱は正常事象である。必要なcounterはguard実装時に用意し、存在しないmetricを利用済みとは報告しない。

rollbackは人の `/voice leave` またはDiscord disconnect → staging Deploymentを0 replica → 専用Secretの利用停止/必要時token revoke → staging限定egress削除 → staging application停止/削除。emptyDirはPod削除で破棄し、試験者側のbot role/inviteも運用者が解除する。本番HermesのPVC/Secret/Deploymentへ触れない。本番導入を提案する際には、その時点のimage digestとconfigの復旧手順を別PRに添える。
