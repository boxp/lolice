# BOXP-729: Alloy のログ収集停止の根本原因と、欠測検知・再発防止

## 目的

BOXP-728 で復旧した golyat-2/3 の Alloy 停止（2026-11-24 07:50〜11-27 05:29 UTC）について、
Loki の一時停止後に収集が戻らなかった原因を、確かめたことと推定を分けて示す。
そのうえで「Pod は動いていてログも出ているのに Loki に届かない」を通知し、再発を防ぐ。

## 調査結果

### 確かめたこと（本番の Prometheus / Loki と隔離再現）

1. Loki の停止は 2026-11-24 07:46:30〜07:49:30 UTC（Alloy の push が 502/500/接続失敗）。loki-0 が golyat-2 から golyat-3 へ作り直された。
2. その 3 分間に **収集対象の Pod が消えたノードは golyat-2 と golyat-3 だけ**（kube_pod_info）。
   - golyat-2: loki-0 自身が 07:46 に削除された。
   - golyat-3: descheduler の Job Pod が 07:48/07:50/07:52 に終わり、loki-0 は 07:46:30 に作られてから 07:47 に作り直された。
   - 他の 5 台: 同じ時間に消えた Pod は無い（golyat-1 は追加だけ）。この 5 台は全部復帰した。
3. golyat-2/3 の Alloy は 07:50 以降、読取行数・送信件数とも 0、active files は 91/136 のまま、
   up=1、設定の hash は 11-27 03:00 の設定変更後も古いまま（他 5 台は新しい hash）。
4. **docker だけの隔離再現**（`repro-alloy-stall.sh`、本番には触れない）で、Loki を止めている間に
   収集対象ファイルを 1 つ消すと、Alloy v1.7.0 は Loki が戻っても残りのファイルを送らなくなった。

   | 版 / 条件 | 試行 | 再開 30〜60 秒の a.log 到達行数 | 判定 |
   |---|---|---|---|
   | v1.7.0 / 停止中に削除 | 1 | 210 | 詰まり（正常の約 4%） |
   | v1.7.0 / 停止中に削除 | 2 | 0 | 完全停止 |
   | v1.7.0 / 削除なし（対照） | 1, 2 | 5,100 / 4,980 | 正常 |
   | v1.22.0 / 停止中に削除 | 1, 2 | 5,078 / 5,040 | 正常 |

   （a.log には 30 秒で約 6,000 行書く。結果ログ: `/home/boxp/verification-artifacts/BOXP-729/repro-alloy-stall/`）

### 推定（確度高、ただし本番の goroutine dump は無い）

Alloy v1.7.0 の `loki.source.file` の不具合 [grafana/alloy#3488](https://github.com/grafana/alloy/pull/3488)
（v1.8.3 で修正。報告 [#3456](https://github.com/grafana/alloy/issues/3456) は「8 台中 2 台が止まり、config reload も止まる」）。
対象が消えると、止める tailer が未送信分を内部 channel へ流そうとするが、その channel を読む Run ループは
mutex を握ったまま tailer の停止を待っているので、どちらも進まなくなる。Loki が止まっていると未送信分が
溜まっているので、この組み合わせ（Loki 停止 × 対象の削除）で起きやすい。

本番で詰まった Alloy の goroutine dump は取れていない（再起動済み、読取専用権限のため API にも届かない）ので、
「本番の停止がこの経路だった」は時系列の一致と隔離再現による推定に留まる。

## 対応

1. **再発防止**: Alloy を v1.22.0 に上げる（chart 0.12.0 のまま `image.tag` だけ）。#3488 のほか、
   #4376（パイプライン詰まり中の更新が遅い）、#4585（停止時の deadlock）、#7538/#7641 などの loki.source.file の修正を含む。
   - 設定は v1.22.0 の `alloy validate` を通る。非推奨で validate が落ちる `env()` は、v1.7.0 でも既に使っている `sys.env()` に揃えた。
   - 副作用: DaemonSet が 1 台ずつ再起動し、読取位置 (/tmp/alloy) が消えるので、ノードに残っているログを先頭から読み直す
     （BOXP-728 と同じ。Loki は保存済みの古い重複を 400 で捨てる）。
2. **欠測検知**（原因と独立に安全）: `argoproj/prometheus-operator/log-ingestion-gap-rules.yaml`
   - `AlloyNodeLogDeliveryStalled`: そのノードのコンテナログ（kubelet 計測の使用量）が直近 15 分に変化（増加またはローテーション）しているのに、そのノードの Alloy の送信が 15 分 0 件で、他ノードは送れている。15 分続いたら warning。
   - `AlloyLogDeliveryStalledClusterWide`: 全 Alloy が 15 分 0 件で、ログは出ていて（使用量が変化）、Loki は replicas 0（計画停止）ではない。
   - `AlloyConfigNotApplied`: 1 Pod だけ設定の hash が多数派と違うまま 30 分。
   - 仕様: Obsidian `Projects/log-ingestion-gap-alert/spec.md`。テストは仕様の例 1〜9 を promtool で。
   - 本番データでの後追い確認: 11-20〜11-24 07:55 の正常期間は誤検知 0、11-24 08:05 から golyat-2/3 だけ条件成立（for 15m で 08:20 ごろ発報相当）、復旧後 05:30 に解消。

## 検証

- promtool: 仕様の例 1〜9（ルール空で失敗を確認してから実装）。
- CI の chainsaw（kind）で v1.22.0 の Alloy が本番 values / ConfigMap でログを Loki に届けること。
- merge 後: ArgoCD 反映、全 7 台が v1.22.0 で送信 > 0、config hash 一致、ルールが Prometheus に読み込まれ、どのアラートも出ていないこと。
- 故意の本番停止試験はしない。

## Lean / 仕様の手順

Lean モデルは作らない。順序に依存する規則ではなく時点ごとの条件の and と `for` で、promtool の単体テストで直接確かめられるため。
