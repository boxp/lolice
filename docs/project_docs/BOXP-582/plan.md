# BOXP-582: 通知 channel 試作を正式な回帰テストへ移管し plugin の二重管理を解消する

## 現状（2026-11-17 調査）

- plugin 正本は lolice `argoproj/hermes-agent/plugins/boxp-notify/`（ConfigMap → init container で配備）。
  arch `prototypes/hermes-notify-channel/plugin/boxp-notify/` は byte 単位で同一の複製で、配備には使われていない。
- arch `prototypes/hermes-notify-channel/lab/` に隔離試験がある。
  - `unit_check.py`: image 不要。96 項目（重複防止、`both` 片側障害の回復、送信中回復との競合、別 process 排他、
    分割送信再開、長い宛先名、Alertmanager の種類別設定・回復・期限切れ、cron 宛先書換え・移行）。archの複製を読む。
  - `stage2.sh`（+ `stage2-context.bb`, `Dockerfile.stage2`, `stub.bb`, `home.bb`, `tick.sh`）: 本番 image と lolice の
    manifest・init script、無改変の dispatcher（arch `docker/codex-workspace/task-board/task_board_events.bb`）で、
    Task Board 正式報告・再投入・無通知・例外・静音保留→朝 digest・家へ書けない・routing 切戻し・両方不通＋再起動・
    旧 route 拒否・cron 成功/失敗/同本文・Alertmanager・画像 route・cron 宛先書換え/移行・plugin validate を流す。
    判定は人が証跡を読む方式（前回基準: 家 11 件・Telegram 8 通）。
  - `stage1.sh`（Alertmanager 段階 1）、`lab.sh`/`suite.sh`/`scenarios.sh`/`recovery.sh`/`cron.sh`（上流 image＋lab 設定、段階 0）、
    `cron-standalone.sh`、`home-dispatch.sh`。
- lolice CI（`chainsaw.yaml` の "Hermes plugin unit tests"）は `plugins/*/test_*.py` を自動実行する。boxp-notify には test が無い。

## 方針

1. lolice に正式な試験を置き、試験は正本 plugin を直接読む（arch の複製を不要にする）。
   - `argoproj/hermes-agent/plugins/boxp-notify/boxp_notify_check.py`（旧 unit_check.py、読込先を同じ dir の `__init__.py` に変更）と
     `test_boxp_notify.py`（subprocess で実行し全項目 ok を確認）。既存 CI step が自動で拾う。configMapGenerator は
     file を列挙しているので配備物は変わらない。
   - image を使う統合試験を `tests/hermes-agent/notify-lab/` へ移す。plugin・manifest は lolice の checkout から、dispatcher は
     arch checkout（引数）から読む。証跡を機械判定する検査 script を追加し、人の目視だけに頼らない。
   - 統合試験の GitHub Actions workflow（plugin / lab / hermes-agent manifest 変更時の PR と手動実行）。外部通信は image 取得のみ、
     container は `--network none`。本番へは何も送らない。
   - 依存（上流/本番 image、bb、actions）は Renovate で追えるよう固定値＋custom manager に寄せる。
2. arch の `prototypes/hermes-notify-channel/` を削除する（lolice 側の merge・main CI 確認後）。
3. 本番配備経路（ConfigMap 生成、deployment、Hermes image）は変更しない。kustomize build の差分が無いことを確認する。

## 安全境界

通知配送挙動・宛先・認証・静音・重複回復・秘密非公開を変えない。本番へ試験通知や偽 alert を送らない。
本番データ/receipt/履歴の削除、権限・課金追加、Hermes 本体 patch 追加はしない。検証画像は vault に置かない。
