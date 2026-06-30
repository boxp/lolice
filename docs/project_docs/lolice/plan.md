# Plan: skill-symlink-agents-codex

## Objective
`.claude/skills` にあるスキルを codex/pi agent でも利用できるよう `.agents/skills/` にシンボリックリンクを作成し、不要な `.pi/agent/skills/` を削除する。

## Changes
1. `./.agents/skills/lolice-image-updater -> ../../.claude/skills/lolice-image-updater` シンボリックリンク追加
2. `./pi/agent/skills/lolice-image-updater` 削除（旧パス）
