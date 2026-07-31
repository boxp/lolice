# codex-workspace: IS01(WiFi) からの TCP 到達性修正 — 明示 NodePort 追加

## 背景 / 問題
IS01(Sharp/au, mainline Linux, WiFi 192.168.10.11) から codex-workspace pod への
対話端末(is01jpterm)は、pod の tmux を IS01→pod SSH(dbclient) で読み書きする。
この IS01→pod 接続が LoadBalancer VIP `192.168.10.98:22/3456` に対して**TCP のみ
タイムアウト**し、対話ができない。

## 実機切り分けで確定した真因
| IS01→ 宛先 | MAC | ICMP | TCP |
|---|---|---|---|
| 192.168.10.98 (LB VIP, codex-workspace) | 02:00:6f:18:43:62 (kube-vip 仮想) | ○ | **×(timeout)** |
| 192.168.10.99 (kube-vip API VIP) | 02:00:a0:2e:c3:8b | ○ | ○ |
| 192.168.10.102/103/104 (control-plane node 実IP) | 02:00:b3:... 等 | ○ | ○ (400) |
| 192.168.10.100 (LAN 有線ホスト) | 実MAC | ○ | ○ |

- **有線ホスト・node 実IP・API VIP は TCP 到達可。LB VIP .98 だけ TCP 不通。**
- WSL は Windows(192.168.10.100, 有線) の NAT 経由なので .98 に届く(=同一サブネット
  非対称ではない)。IS01 は WiFi 直で、LB VIP の仮想 MAC 宛 TCP が WiFi AP の
  FDB/ブリッジで落ちる(常時トラフィックを出さない VIP MAC は学習が切れる)。
- externalTrafficPolicy を変えても直らず、`Local` はむしろ既存 VIP 経路を壊すリスク。

## 修正方針(最小・追加のみ)
codex-workspace Service に**明示 NodePort** を付与し、IS01→pod は**node 実IP:NodePort**
（実測 TCP 到達可能な 192.168.10.102 等）に当てる。LoadBalancer/既定 Cluster policy は温存。

- service.yaml: ssh に `nodePort: 30022`、even-terminal に `nodePort: 30456` を追加。
- 既存の LB VIP(.98)・cloudflared・WSL 経路は無変更(純粋な追加)。

## 検証
1. ArgoCD 同期後、IS01 から `dbclient -y -y -i /tmp/id_db -p 30022 boxp@192.168.10.102 'hostname'`
   が pod hostname を返すこと(直接オラクル)。
2. NetworkPolicy が nodePort の SNAT ソース(node の Calico tunnel IP)を drop する場合のみ、
   ingress に該当 tunnel IP を追記(既存 7 個の 192.178.x /32 でカバーされる想定。まず無改変で試す)。
3. is01jpterm の接続先を VIP→node実IP:30022 に変更し、日本語対話端末を実機実証。

## ロールバック
service.yaml から nodePort 行を削除するだけ(既存挙動に完全復帰)。
