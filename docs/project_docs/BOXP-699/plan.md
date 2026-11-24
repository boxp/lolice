# BOXP-699: onitiku Discord へのアラート通知を対象限定で停止する

## 依頼

Owner「onitiku discordに送ってるアラートを止めたいです」。監視評価・ログ収集・サービス本体・他宛先 (メール / Hermes / 他 Discord) は止めない。

## 変更前 (2026-11-24 18:1x UTC 本番読戻し、read-only)

| 項目 | 状態 |
| --- | --- |
| Grafana contact point | `boxlab alert` (discord), `email receiver` (email), `onitiku server` (discord)。すべて UI 作成 (provenance なし) |
| default notification policy | receiver=`boxlab alert`、子 route なし |
| `onitiku server` を receiver にするルール | `onitiku palworldが稼働していません` (uid be92yxv4y2mf4b, 稼働中, Normal) / `ark asa server restarts count` (uid fenkaqm8slj40d, paused) |
| mute timing / silence | 0 件 |
| Prometheus Alertmanager | Discord 受信者なし (Critical/Warning=メール、critical/notify_hermes=Hermes)。今回変更しない |
| Grafana SA (codex-workspace) | alerting は read のみ。UI/API での書き換え権限なし → GitOps で行う |

## 方針

Grafana alerting の file provisioning (`argoproj/prometheus-operator/grafana-alerting/onitiku-discord-stop.yaml`) で

1. 常時有効な mute timing `onitiku-discord-stopped` (weekdays `sunday:saturday`) を作る
2. 上記 2 ルールを **同じ uid** で取り込み、`notification_settings.mute_time_intervals` に付ける。定義は本番 export と同一 (差分は folder 名と mute のみ)

これで

- ルールの評価・状態 (Normal / Alerting) と Grafana 上の表示は残る
- FIRING / RESOLVED のどちらも `onitiku server` に送られない (mute は route 単位で通知全体を抑止)
- receiver は `onitiku server` のままなので default policy (`boxlab alert`) へも流れない
- contact point `onitiku server` (Discord webhook) は削除しない
- 2 ルールは provenance=file になり UI から編集できない。Git が正本になるので UI 側の再適用で通知が復活しない

不採用: ルールの pause (評価が止まる)、receiver の削除 (default の `boxlab alert` に流れる)、contact point の削除・上書き (共有物を無確認で消す / webhook secret が必要)。

## 検証

- `tests/chainsaw/prometheus-operator/onitiku-discord-mute/` (CI の chainsaw で prometheus-operator 変更時に実行)
  - 本番と同じ Grafana 13.2.2 に UI 作成相当のルール / contact point (webhook) / policy を作り、対照として FIRING / RESOLVED が onitiku に届くことを確認
  - provisioning を mount して同じ PVC で再起動 → 起動、同 uid・provenance=file・mute 付与、contact point / policy 不変、folder 重複なし
  - 偽 Prometheus で発火→解消させ、onitiku / boxlab とも通知 0 件
  - 再起動後も同じ状態
- ローカル kind で全 step 成功。最初の版 (`monday:sunday`) は Grafana が "start day cannot be before end day" で CrashLoop することをこのテストで検出し修正済み
- 配備後: Argo CD sync、Grafana Ready・restart 0、API で 2 ルールの provenance/mute、palworld ルールの評価継続、contact point と policy の不変を読戻す

Spec-first / Lean: 設定変更のみで状態機械のルールを新設しないため Lean モデルは作らない。期待結果はチケットの要求 (FIRING/RESOLVED/fallback の漏送なし) から決め、テスト header に記述。

## 範囲外 / 要確認

- `ark-discord-bot` (ARK サーバー状態変化を Discord チャンネルへ自動投稿する bot) は Grafana の経路外で、サービス本体の変更になるため今回触らない。onitiku Discord 宛てでこれも止めたいかは Owner 確認事項
- 通知を再開する場合は各ルールの `mute_time_intervals` を外す PR を出す
