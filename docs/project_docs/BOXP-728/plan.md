# BOXP-728: golyat-2/golyat-3 の Alloy 停止の原因と復旧

## 調べてわかったこと（2026-11-27 時点、Prometheus/Loki の値）

- 2026-11-24 07:46:02 UTC に loki-0 が SIGTERM を受けて止まり、golyat-3 上で作り直された。
  この間 Loki は `500 empty ring` / `502` を返し、すべての Alloy が送信を再試行した。
- 他の 5 台は数分で送信を再開した。golyat-2 (grafana-alloy-9q72m) と golyat-3 (grafana-alloy-grjc5) は
  07:50 ごろに `loki.source.file.pods` の評価に 335 秒/349 秒かかったあと、評価待ちが 2〜3 件たまったまま止まった。
  ファイル読取 (`loki_source_file_read_lines_total`) と送信 (`loki_write_sent_entries_total`) はどちらも 0 のまま。
- 設定の再読込も止まっていて、`alloy_config_hash` は古い 95a9… のまま（他の 5 台は 938a…）。
  動いている component は 6 個（他の 5 台は 10 個）で、BOXP-721/726 の監査用 component が起動していない。
- Alloy は v1.7.0。送信先が止まっている間に収集対象のファイルが入れ替わると（このノードでは descheduler などで
  2 分ごとに新しい Pod ができる）、`loki.source.file` の更新が終わらずに詰まった、と判断した。
  Alloy 自身のログは同じノードで集めているため 07:46 以降は残っておらず、詰まった箇所はメトリクスからの推定。

## 復旧方法

- この作業用アカウントは Pod の削除もログの取得もできず、Alloy の API にも届かない。そこで GitOps で
  `controller.podAnnotations` に再起動用の値を追加し、DaemonSet を 1 台ずつ順に再起動する（maxUnavailable=1）。
- 設定（configmap）と監視の方針は変えない。他の 5 台も再起動されるが、設定は同じまま。
- 読取位置は `/tmp/alloy`（コンテナ内）にあり、再起動すると消える。そのため各ノードは今あるログファイルを
  先頭から読み直す（これまでの再起動と同じ動き）。golyat-2/3 では、ローテートで消えていない分の欠測期間の
  ログが、Loki の受付期限（既定 7 日）内なら取り戻せる。他のノードでは同じログが重ねて送られる。

## 復旧したことの確かめ方

- `sum by (pod)(rate(loki_write_sent_entries_total[10m]))` が新しい 2 つの Pod で 0 より大きい
- `alloy_config_hash` が全台で同じ値、`alloy_component_controller_running_components` が他の台と同じ数
- Loki で `{node=~"golyat-2|golyat-3"}` の新しいログが届いている。監査用ストリームも届いている
- 11-24〜11-27 の期間を、golyat-2/3 の Pod のログでどこまで取り戻せたかを数えて報告する

## 仕様先行・Lean を使わない理由

設定値を 1 つ足して再起動させるだけで、新しい動作や状態の規則が無いため。
