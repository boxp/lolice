# Alertmanager のメール通知 (Resend 経由)

## 背景

2026-08-01 20:50:45 UTC に control-plane ノード `shanghai-1` が microSD の I/O 破綻で
ハングし、**3日間** 無応答のままだった。詳細は
boxp/arch の `docs/project_docs/shanghai-node-resilience/plan.md`。

### 3日間気づかなかった理由: 通知経路が存在しなかった

アラートルールは存在し、**実際に発火していた**:

| アラート | severity | 発火 |
|---|---|---|
| `ControlPlaneNodeNotReady` | critical | 2026-08-01 21:00 UTC（死亡の約10分後） |
| `KubeNodeNotReady` | warning | 21:30 UTC |
| `KubeNodeUnreachable` | warning | 21:30 UTC |
| `KubeletInstanceUnreachable` | warning | 21:30 UTC |

いずれも復旧させるまで3日間鳴り続けていた。しかし Alertmanager の実稼働設定
(`alertmanager-main-generated`) は kube-prometheus 既定のままで、

```yaml
receivers:
- name: Default
- name: Watchdog
- name: Critical
- name: "null"
```

と **すべて名前だけで通知設定を持たない**。Alertmanager が黙って捨てていた。
**監視は正常に動作していたが、通知経路が存在しなかった。**

### なぜこれが最優先か

本命の恒久対策である「etcd を microSD から外す (USB SSD)」は
**ハードウェア制約により対応不可**と判断された (2026-08-05)。
したがって ~64 GiB/日 を microSD に書き続ける構造は変わらず、
**ノードは今後も同じ壊れ方をする。**

変えられるのは「10分で気づいて自動復帰するか、3日止まるか」であり、

- 自動復帰 → watchdog + hung_task_panic (boxp/arch#11944 / #11945 で対応済み)
- 気づく   → **本件**

の2つが実質的な対策になる。

## 方針

送信基盤は **Resend** を使う。`terraform/cloudflare/b0xp.io/lolice-member-portal`
で既に利用実績があり、新しい送信経路を増やさずに済む。

Resend は SMTP インターフェースを提供しているため、Alertmanager の `email_configs`
からそのまま利用できる。

| 項目 | 値 |
|---|---|
| smarthost | `smtp.resend.com:587` (STARTTLS) |
| auth_username | `resend` (固定文字列) |
| auth_password | Resend の API キー |
| from | Resend で**検証済みドメイン**のアドレスであること |

通知対象は **critical のみ**。kube-prometheus 既定のルートに
`severity = critical -> Critical` が既にあるため、`Critical` receiver に
`email_configs` を足すだけで済む。warning まで広げるとノイズで無視するようになるため、
まず確実に届く状態を作ることを優先する。

## 対になる変更 (boxp/arch)

`terraform/aws/alertmanager/` を新設し、SSM パラメータを3つ定義する。

| パラメータ | 用途 |
|---|---|
| `/lolice/alertmanager/RESEND_API_KEY` | SMTP パスワード |
| `/lolice/alertmanager/ALERT_EMAIL_TO` | 通知先 |
| `/lolice/alertmanager/ALERT_EMAIL_FROM` | 送信元 (Resend 検証済みドメイン) |

いずれも `SecureString` + ダミー値 + `lifecycle { ignore_changes = [value] }`。
実値は手動更新する (`terraform/aws/ark-discord-bot/ssm.tf` と同じ方式)。

**通知先アドレスと送信元アドレスも SSM 経由にしている。** public リポジトリに
個人のメールアドレスを置かないため。

## 変更内容 (本リポジトリ)

- `argoproj/prometheus-operator/external-secret-alertmanager.yaml` (新規)
  ExternalSecret が SSM から値を取り、`alertmanager.yaml` を丸ごと生成する
- `argoproj/prometheus-operator/overlays/alertmanager.yaml`
  Alertmanager CR に `configSecret: alertmanager-main-config` を追加

設定本体を ExternalSecret の `target.template` に置くことで、
git 上には秘密情報もメールアドレスも残らない。

## 適用順序

**この順でないと動かない。**

1. 本 PR をマージ → `terraform/aws/alertmanager` が apply され、SSM パラメータが
   ダミー値で作成される
2. **3つのパラメータに実値を手動で設定する** (AWS コンソールまたは CLI)
   ```bash
   aws ssm put-parameter --overwrite --type SecureString \
     --name /lolice/alertmanager/RESEND_API_KEY   --value 're_xxxxxxxx'
   aws ssm put-parameter --overwrite --type SecureString \
     --name /lolice/alertmanager/ALERT_EMAIL_TO   --value 'you@example.com'
   aws ssm put-parameter --overwrite --type SecureString \
     --name /lolice/alertmanager/ALERT_EMAIL_FROM --value 'alertmanager@b0xp.io'
   ```
3. boxp/lolice 側の PR をマージ → ArgoCD が同期し、Alertmanager が新しい設定で起動

先に 3 をやると ExternalSecret が同期できず `alertmanager-main-config` が作られないため、
Alertmanager は既存の設定のまま変わらない (停止はしない)。

## 検証項目

- [ ] `kubectl -n monitoring get externalsecret alertmanager-config` が `SecretSynced`
- [ ] `kubectl -n monitoring get secret alertmanager-main-config` が存在する
- [ ] Alertmanager Pod が新しい設定で起動する (`amtool check-config` は事前検証済み)
- [ ] **テストアラートを発火させ、実際にメールが届くこと**
- [ ] Alertmanager のログに SMTP エラー (認証失敗 / From ドメイン未検証) が無いこと

最後の2つが本質。設定が入っただけでは「届かないアラート」を作り直すだけになる。

## 適用時に踏んだ罠 (2026-08-05)

SSM に実値を設定し #763 をマージしたところ、ExternalSecret が
`SecretSyncedError` になり Secret が作られなかった。

```
could not apply template: ... unable to parse template at key alertmanager.yaml:
template: alertmanager.yaml:60: missing value for command
```

**原因: コメント行に書いた二重波括弧。**
`alertmanager.yaml` はブロックスカラー (`|`) なので、`#` で始まる行も
YAML のコメントではなく**文字列の一部**として ESO の Go template に渡る。
そこに空の波括弧があると「空アクション」として実行され、テンプレート全体が
parse に失敗する。皮肉なことに、テンプレートのエスケープに注意しろという
コメント自体がテンプレートを壊していた。

Alertmanager は既存設定のまま動き続けたので停止はしていない (設計どおり)。

### 再発防止

`scripts/verify-alertmanager-template.py` を追加した。
テンプレート中の波括弧アクションを列挙し、

- `{{ .foo }}` の形以外が含まれていないか
- 使われている変数が `spec.data` に定義されているか
- 展開後に波括弧が残っていないか
- 展開後が妥当な YAML か

を検証する。**YAML として parse するだけでは足りない**というのが今回の教訓で、
実際に修正前のファイルに対して FAIL することを確認済み。

## 残課題

- 高耐久 microSD への交換 (shanghai-1 のカードは write await が兄弟機の4倍まで劣化)
- 2026-07-25 19:00 UTC の書き込み倍増 (27→64 GiB/日) の犯人特定。
  etcd メトリクスが取れるようになったので追跡可能
