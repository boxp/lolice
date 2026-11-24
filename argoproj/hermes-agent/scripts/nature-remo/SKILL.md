---
name: nature-remo
description: "Use when the Owner asks about home appliances (air conditioner, ceiling fan) via Nature Remo: list appliances/signals, read room sensors, or send an Owner-confirmed command."
---

# Nature Remo（エアコン・シーリングファン）

公式 Cloud API を `/opt/data/scripts/nature_remo.sh` で直接呼ぶ。MCP は使わない。
現行仕様: vault `Projects/kohako-nature-remo/spec.md`。

## 読み取り（いつでも可）

- `nature_remo.sh status` … トークンの有無・許可済み家電（通信なし）。終了3なら未設定。
- `nature_remo.sh list` … 家電と信号の id 一覧。エアコンの「Remoが最後に送った設定」は実機の現在状態ではない。
- `nature_remo.sh sensors` … Remo 本体の室温・湿度など（測定時刻つき）。

## 送信（本人の明示の指示があるときだけ）

1. まず `--execute` なしで実行し、「未送信（確認用）」の内容を本人に見せて確認をもらう。
2. 本人が了承したら、同じ命令に `--execute` を付けて **1 回だけ** 実行する。
   - `send --appliance <家電id> --signal <信号id>`（シーリングファンなどの赤外線信号）
   - `aircon --appliance <家電id> [--power on|off] [--mode cool|warm|dry] [--temp 26]`
3. 結果の伝え方:
   - 「送信受付」= Remo API が受け付けただけ。家電が動いたかは未確認と必ず添える。
   - 「送信結果不明」(終了7) / 「拒否」(終了4/5/6) のときは **自分で再実行しない**。理由をそのまま本人に伝える。
   - 「トグル信号」の注意が出たら、今の状態を本人に確かめてから送る。

## してはいけないこと

- 自動の再試行・連打・ループ・cron からの送信。
- 許可リスト（`/etc/nature-remo/allowlist.edn`、lolice の PR でだけ変わる）にない家電・信号を送ろうとすること。
- トークンの値を表示・保存・転記すること。トークン未設定なら「本人が home.nature.global で発行し、
  SSM の `/lolice/hermes-agent/nature-remo-token` に保存する必要がある」と伝える。
