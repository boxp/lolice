# Hermes 通知回帰試験

plugin 正本 `argoproj/hermes-agent/plugins/boxp-notify` の通知回帰を検証する。
BOXP-546 で channel を試作し、BOXP-553/554 で Alertmanager と Task Board・cron を検証した。
BOXP-556 で本体 patch の撤去と画像 route、cron 移行を検証した。

## channel の動き

届け先の指定（webhook route の `deliver: boxp_notify` と `deliver_extra.chat_id`、cron の `deliver: boxp_notify:<名前>`）:

| chat_id | 種類 | 家の受信箱 |
|---|---|---|
| `tb:<event id>` | Task Board 通知 | `Projects/task-board-events/delivered-copies/<event>[.notice].md` |
| `direct:alertmanager` | agent を通さない直送 | `Projects/alertmanager-deliveries/delivered-copies/alert-*.md`（BOXP-501 の形式。title と発生件数は relay の本文から読む。`direct:alertmanager:<発生件数>` も可） |
| `direct:<その他>` | agent を通さない直送 | 無し（受信箱の取り決めが無いので Telegram のみ） |
| それ以外 | cron | `Projects/cron-deliveries/delivered-copies/cron-*.md` |

- `send` が実配信。まず家の受信箱へ書き、書けない時だけ Telegram へ送る（`BOXP_NOTIFY_TELEGRAM_MODE=fallback`。`off` / `both` も可）。
  直送は名前ごと、cron は `BOXP_NOTIFY_TELEGRAM_MODE_CRON` で上書きできる。本番は Alertmanager と cron が `both`（家と Telegram の両方）。
- Task Board 通知は設定の mode ではなく、本体 patch と同じ取り決めで届け先を決める（dispatcher が読むファイルも同じ）:
  - 正式報告（Notes に `result=notified|question` の記録があり、本文に `event=<id>` がある）は、
    `Projects/task-board-events/delivery-routing.md` が `task-board: home` で、催促 route が登録済み・dispatcher の
    準備状態（`home-reminder-ready.md`）が新しい間だけ、家の受信箱だけへ届ける（写しに `primary: home`。未読の催促と
    家へ入らなかった時の Telegram 送信は dispatcher が行う）。受信箱へ書けない時は Telegram へ送り、写しは後から印なしで書く。
  - 照合通知（配送要確認）、記録の無い報告、routing が `telegram` の時は Telegram へ送り、家へ印なしの写しを書く。
  - 静音時間（dispatcher が公開する `quiet-policy.md`）、digest が引き取った event（`delivery-claims/`）、夜間に
    `deferred` とした event の報告は Telegram へ送らず、`delivery-receipts/<event>.md` に `quiet-held` を残す（朝の digest が回収）。
    BOXP-614 以降、保留した本文は家の受信箱へ印（`primary`）なしで保存する（夜間も読めて返信でき、催促・Telegram・プッシュはしない。digest 自体は保存しない）。
  - 届けたら `sent`、どこにも届かなければ `uncertain`、記録つきの無通知は `silent` を `delivery-receipts/` に残す。
    dispatcher はこれで event を完了・保留・再送停止にする。
- cron は job id しか渡らないので、家の受信箱の job 名は scheduler の `cron/jobs.json` から読む。
  Telegram へは、本文が 1 通に収まる時は Markdown（太字・code・link・見出し）を HTML に直して送り、拒否されたら素の本文で送る。
- Telegram の宛先は `BOXP_NOTIFY_TELEGRAM_TOKEN` / `_CHAT_ID` / `_THREAD_ID`。無ければ gateway 自身の
  `TELEGRAM_BOT_TOKEN` / `TELEGRAM_HOME_CHANNEL` / `TELEGRAM_HOME_CHANNEL_THREAD_ID`（`deliver: telegram` と同じ届け先）。
- 送る前に永続 receipt（`$HERMES_HOME/boxp-notify/receipts/`）を書き、同じ鍵は 1 回だけ届ける。
  Task Board は `event id × (report|notice)`、cron と直送は `名前(job id) × 本文 × 直近の受理` が鍵。
  記録と受信箱のファイル名は「読める接頭辞＋鍵全体の hash」で、宛先名の長さに依らない。
  直送と cron は、同じ本文が「その宛先（job）の直近の通知」で、時間幅の内（直送 20 分、cron は
  `BOXP_NOTIFY_CRON_DEDUP_SECONDS`、既定 300 秒）にある時だけ同じ通知の再送として扱う。cron は実行 ID が渡らないので、
  同じ job が時間幅の内に同じ本文を続けて出すと 1 件にまとまる（job の最短間隔より短くしておく）。
  job id が渡らない時（gateway 外配送の口）は job を区別できないので、再送扱いをせず毎回届ける（別 job の同じ本文を落とさないため。重複は起こり得る）。
- 無通知にするのは、票 Notes に helper の `result=silent|deferred|continued` の receipt がある時だけ。channel に `allow_silence` は与えない。
- `event=<id>` を含まない本文（上流の例外文・空応答の代替文など）は「配送要確認」の notice として 1 回だけ届ける。上流の文言には依存しない。
- 家にも Telegram にも届けられなかった分は pending として残り、`connect` 後の定期回復で届ける。
  `both` では家と Telegram の両方へ届くまで pending のまま残し、届いていない側だけを回復で送る（片方でも届けば上流へは成功を返す）。
  片方へ届いたまま、その届いた時点から 24 時間（`BOXP_NOTIFY_PENDING_MAX_AGE_SECONDS`）もう片方へ届かない分は `expired` にして再試行をやめる（作成時刻からではないので、両方が長く止まった後に片方だけ戻っても、もう片方の回復は続く）。
  どちらにも届いていない分は期限で捨てない。Task Board の event は、配送係向けの配送記録（`sent`）を vault へ書けていない間は期限切れにしない（記録が無いと配送係が event を完了できないため、vault が戻るまで続ける。送信済みの通知は再送しない）。
- `send` と回復は receipt ごとの同じ lock（process 内の lock ＋ lock ファイル）を取る。送信中の通知を回復が重ねて送ることは無い。

## 実行方法

単体（image 不要）:

```sh
python3 -B -m unittest discover -s argoproj/hermes-agent/plugins/boxp-notify -p 'test_*.py'
```

統合（Docker と bb が必要）: 候補 image を取得し、arch checkout を dispatcher に使用する。
Renovate が arch の `docker/hermes-agent/Dockerfile` の FROM を更新した際は、候補 image を arch の通常の build 手順で作成し、以下の IMAGE に指定する。

```sh
export ARCH_DIR=/path/to/arch
IMAGE=ghcr.io/boxp/arch/hermes-agent:latest
docker pull "$IMAGE"
docker image inspect "$IMAGE" --format '{{json .RepoDigests}}'
tests/hermes-agent/notify-lab/stage2.sh build notify-lab "$IMAGE"
tests/hermes-agent/notify-lab/stage2.sh run notify-lab notify-lab evidence | tee evidence.log
bb tests/hermes-agent/notify-lab/check-stage2.bb evidence
```

run のログは `evidence.log`、validate は `evidence.validate.txt` に保存する。stage2 の第3引数で manifest dir、第4引数または `ARCH_DIR` で arch checkout を指定できる。段階1は `stage1.sh build <tag> <image> [manifest-dir]`、`stage1.sh run <tag> <name> <evidence-dir>`。段階0は `lab.sh build <tag> <image>`、`suite.sh <tag> <name> <evidence-dir>`。個別の `scenarios.sh`、`recovery.sh`、`cron.sh`、`cron-standalone.sh` も利用できる。`home-dispatch.sh <evidence-dir> <work-dir>` はテスト用 helper を含む完全な arch checkout を `ARCH_DIR` に必要とする。

コンテナは `--network none`、Telegram と LLM は loopback stub を使用し、本番には何も送らない。

## カバレッジ

| 項目 | 単体 | 統合 | 未検証 |
|---|---|---|---|
| 重複防止・process 排他 | ✓ | ✓ | |
| 片側障害・両側障害と回復 | ✓ | ✓ | 本番実通知 |
| 静音・digest | ✓ | ✓ | |
| Task Board / cron / Alertmanager | ✓ | ✓ | job id なし配送の image 実測 |
| cron 宛先書換え・移行 | ✓ | ✓ | 移行前の実配送 |
| 画像 route | | ✓ | 本番の実画像 |
| standalone sender | ✓ | | 上流からの呼出 |

## 制限・割り切り（試作時から継続）

- gateway 外配送用の `standalone_sender_fn` は登録してあるが、実測した 2 版では `hermes cron tick` が adapter を自分で起こして `send` を呼ぶため、この口は呼ばれなかった（未実測）。
- `lab-fail-home` / `lab-no-recovery` の印ファイルは障害を起こすための実測用。
- Telegram 送信は 4000 字で分割するだけで、再送間隔の調整は無い。書式変換は Task Board と cron の 1 通に収まる本文だけ。
- Task Board と cron の通知も channel 宛では Telegram の会話 session へ mirror されない（返信の文脈は家の会話と本文・Notes）。
- cron job の届け先は job ごとの設定。agent が作る job は、家の Telegram を正確に指す届け先だけ hook が channel 宛へ書き換える（BOXP-556）。それ以外の方法で家の Telegram 宛に作られた job は回復 loop が channel 宛へ移す。別のチャット・トピック・platform 宛の job は移さず、家の受信箱には載らない。移す前（最長で回復間隔）に実行された分は Telegram にだけ届き、家の受信箱には載らない（本体 patch 撤去後）。
- 起動時自己検査は結果を journal と gateway の log（warning）に書くだけで、監視へは繋いでいない。
- Alertmanager 通知は channel 宛になったので、Telegram の会話 session への mirror（`mirror_to_session`）は無い。

## 既知の差・未確認

- cron 通信不能時の文言は上流に依存し、同一 job の連続失敗の2回目が上流から渡らない場合がある。
- Telegram 宛で CLI 作成した job は回復処理の移行まで最大60秒、その間は Telegram にだけ届く。会話履歴への mirror はない。
- job id がない配送口は単体検証のみ。本番 image の起動・復帰、家と G2 の実会話、実通知の到達は未確認。
- 本番の既存 job の失敗通知先の移行は配備後に確認する。
