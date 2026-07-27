# BOXP-133: hermes-agent コハコのプロアクティブ秘書化 実装プラン

## 実装サマリー

hermes-agent（コハコ）に Gmail・Google Calendar 連携を追加し、cronによる定期的なプロアクティブ通知を実現する。

## スコープ境界（このPRの範囲と後続タスク）

**このPR（BOXP-133 / #752）の範囲:**
- hermes-agent Deploymentのinit containerでスクリプト（quiet_gate.sh / calendar_reminder.sh / email_check.sh / email_check.clj）をPVCへ毎デプロイ配置
- Obsidian vault内のスクリプト（morning_report.clj / calendar_reminder.clj）更新

**後続タスク: BOXP-134**
cron登録（Step A〜D）はhermes-agentダッシュボードまたはTelegram経由の手動操作が必要なため、**BOXP-134** として後続タスクに分離。
このPRがマージ・デプロイされた後にBOXP-134で実施する。

## 変更内容

### 1. Obsidian vault変更（Obsidian Syncで自動反映）

#### `morning_report.clj` （更新）

- Google Calendar: 今日の予定を「【今日の予定】」セクションに追加
- Gmail: 要対応メール（noreply系を除外したunread inbox）を「【要対応メール】」セクションに追加
- 呼び出し: `/opt/data/.venv-google/bin/python /opt/data/skills/productivity/google-workspace/scripts/google_api.py`
- トークン: `/home/boxp/google_token.json`

#### `calendar_reminder.clj` （新規作成）

- 開始まで25〜35分のGoogleカレンダーイベントを検索
- 対象イベントがあれば「📅 30分後に〜が始まります。」と出力
- 対象なし → 完全沈黙（何も出力しない）

### 2. lolice k8s変更（このPR）

#### `argoproj/hermes-agent/deployment.yaml` （更新）

- init container に `calendar_reminder.sh` の配置ロジックを追加
- `/opt/data/scripts/calendar_reminder.sh` が存在しない場合のみ作成（べき等）
- 内容: `quiet_gate.sh` による静音制御 + `bb calendar_reminder.clj` 実行

## 必要な手動作業（hermes-agent cron登録）

以下の cron を hermes-agent（コハコ）に直接登録する必要がある。
Telegram または hermes-agent ダッシュボードから実行すること。

### Step A: 毎時メールチェック cron の復元

```
/schedule add
  name: Hourly actionable-mail check (Kohako)
  schedule: every 60 minutes
  script: /opt/data/scripts/email_check.sh
  no_agent: true
  deliver: telegram
```

- 旧ID `8212921d18d2` と同設定
- `quiet_gate.sh` によって 02:00〜09:00 JST は自動スキップされる

### Step B: カレンダーリマインダー cron の追加

```
/schedule add
  name: Calendar event reminder (30min before)
  schedule: every 15 minutes
  script: /opt/data/scripts/calendar_reminder.sh
  no_agent: true
  deliver: telegram
```

- `calendar_reminder.sh` が `/opt/data/scripts/` に存在することが前提（このPRのdeployで配置される）
- 予定なし時は完全沈黙

### Step C: 夕方サマリー cron の追加

```
/schedule add
  name: Evening summary (weekday 18:00 JST)
  schedule: 0 9 * * 1-5
  prompt: |
    コハコとして、以下を確認して200字以内でご主人さまにまとめをお届けください。
    何も特筆すべきことがなければ沈黙してください。
    1. 今日のGmail未対応メール: /opt/data/.venv-google/bin/python /opt/data/skills/productivity/google-workspace/scripts/google_api.py gmail search "is:unread is:inbox newer_than:1d" で確認
    2. 明日のGoogleカレンダー予定: google_api.py calendar list で明日の予定を確認
    3. Task Boardの未完了チケット: bb /opt/data/skills/note-taking/obsidian-task-board/bin/task-board.bb list-tickets で確認
```

### Step D: Xタイムライン監視 cron（xurl認証設定後に追加）

前提: xurl インストール + OAuth設定完了後

```
/schedule add
  name: X timeline context check
  schedule: every 60 minutes
  prompt: |
    コハコとして、HOME=/opt/data/home xurl timeline -n 10 でご主人さまのXタイムラインを確認し、
    日常的なコンテキスト（予定変更・気分・外出など）を把握してください。
    特記事項がなければ沈黙してください。
```

## morning_report cron プロンプト確認

既存の Morning Report cron (ID: `74a03bb016d3`) のプロンプトにはGmail確認の記述があるが、
`morning_report.clj` の実装が天気+RSSのみだった。今回の更新でプロンプトと実装が一致する。
cron のプロンプト変更は不要。

## xurl 手動インストール手順（ユーザーがpod内で実施）

```bash
# xurlのインストール
curl -fsSL https://raw.githubusercontent.com/xdevplatform/xurl/main/install.sh | HOME=/opt/data/home bash

# OAuth認証
HOME=/opt/data/home xurl auth apps add boxp --client-id <CLIENT_ID> --client-secret <CLIENT_SECRET>
HOME=/opt/data/home xurl auth oauth2 --app boxp boxp
HOME=/opt/data/home xurl auth default boxp

# 動作確認
HOME=/opt/data/home xurl timeline -n 5
```

## デプロイ順序

1. このPRをmainにマージ → ArgoCDが hermes-agent を再デプロイ
2. init container が `/opt/data/scripts/calendar_reminder.sh` を配置
3. Obsidian Sync が `morning_report.clj` と `calendar_reminder.clj` を pod に反映
4. hermes-agent の Telegram または Dashboard から cron Step A〜C を登録
5. morning_report cron を手動トリガーして Calendar・Gmail セクションが出力されることを確認
6. xurl インストール後に Step D を実施

## 動作確認チェックリスト

- [ ] morning_report.clj が Calendar・Gmail セクションを含む出力を生成すること
- [ ] calendar_reminder.clj が予定あり時に通知し、なし時に沈黙すること
- [ ] `/opt/data/scripts/calendar_reminder.sh` が pod 内に存在すること
- [ ] 毎時メールチェック cron が再登録されていること
- [ ] カレンダーリマインダー cron が登録されていること
- [ ] 夕方サマリー cron が登録されていること
