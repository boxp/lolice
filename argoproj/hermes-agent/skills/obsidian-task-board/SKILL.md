---
name: obsidian-task-board
description: Claude / Codex / Pi Agent / hermes-agent workflow for safely listing, showing, creating, updating, deleting, and requesting Codex work for the Obsidian Task Board. Use when the user asks to operate BOXP tickets, Boards/Task Board.md, or Tickets/BOXP-*.md.
---

# Obsidian Task Board

Use this skill only through the bundled helper script. Do not manually edit `Boards/Task Board.md` or `Tickets/BOXP-*.md` unless the helper reports an unrecoverable format problem.

Default vault:

```bash
/home/boxp/Documents/obsidian-headless/BOXP
```

Helper:

```bash
if [ -x "$HOME/skills/note-taking/obsidian-task-board/bin/task-board.bb" ]; then
  TASK_BOARD_HELPER="$HOME/skills/note-taking/obsidian-task-board/bin/task-board.bb"
elif [ -x "$HOME/.claude/skills/obsidian-task-board/bin/task-board.bb" ]; then
  TASK_BOARD_HELPER="$HOME/.claude/skills/obsidian-task-board/bin/task-board.bb"
elif [ -x "$HOME/.codex/skills/obsidian-task-board/bin/task-board.bb" ]; then
  TASK_BOARD_HELPER="$HOME/.codex/skills/obsidian-task-board/bin/task-board.bb"
elif [ -x "$HOME/.pi/agent/skills/obsidian-task-board/bin/task-board.bb" ]; then
  TASK_BOARD_HELPER="$HOME/.pi/agent/skills/obsidian-task-board/bin/task-board.bb"
else
  TASK_BOARD_HELPER="$HOME/ghq/github.com/boxp/dotfiles/.claude/skills/obsidian-task-board/bin/task-board.bb"
fi
```

## Safety Rules

- Treat the Task Board lane as the source of truth for ticket status.
- Keep ticket frontmatter `status` and board card `status::` synchronized with the lane.
- Preserve unspecified ticket fields and existing `## Notes` content.
- Use `--dry-run` before destructive or broad changes.
- `delete` requires `--dry-run` or `--confirm`; prefer dry-run first and report the affected ticket file and board card.
- Do not move Codex requests to `In Progress`. The runner owns that transition.

## Commands

List tickets:

```bash
"$TASK_BOARD_HELPER" list --vault /home/boxp/Documents/obsidian-headless/BOXP --json
"$TASK_BOARD_HELPER" list --vault /home/boxp/Documents/obsidian-headless/BOXP --lane Ready --json
```

Show a ticket:

```bash
"$TASK_BOARD_HELPER" show BOXP-55 --vault /home/boxp/Documents/obsidian-headless/BOXP --json
```

Create a ticket:

```bash
"$TASK_BOARD_HELPER" create \
  --vault /home/boxp/Documents/obsidian-headless/BOXP \
  --title "Short title" \
  --summary "What should be achieved." \
  --lane Backlog \
  --priority medium \
  --assignee boxp \
  --dry-run
```

Update only explicit fields:

```bash
"$TASK_BOARD_HELPER" update BOXP-55 \
  --vault /home/boxp/Documents/obsidian-headless/BOXP \
  --priority high \
  --assignee boxp \
  --summary-file /tmp/summary.md \
  --dry-run
```

Append a timestamped note without deleting existing Notes:

```bash
"$TASK_BOARD_HELPER" append-note BOXP-55 \
  --vault /home/boxp/Documents/obsidian-headless/BOXP \
  --source hermes-agent \
  --note "追加調査条件を確認した。"
```

Request Codex work while preserving the stop lane:

```bash
"$TASK_BOARD_HELPER" request-codex BOXP-55 \
  --vault /home/boxp/Documents/obsidian-headless/BOXP \
  --lane Review \
  --note "レビュー指摘に対応して。CI と codex-review も確認する。" \
  --dry-run
```

Delete dry-run:

```bash
"$TASK_BOARD_HELPER" delete BOXP-55 \
  --vault /home/boxp/Documents/obsidian-headless/BOXP \
  --dry-run
```

## Codex Request Workflow

- Backlog: requirements grooming request. Keep the card in `Backlog`, set `assignee: codex`, and append the request to Notes.
- Ready: implementation start request. Keep the card in `Ready`, set `assignee: codex`, and append the request to Notes.
- Review: review feedback request. Keep the card in `Review`, set `assignee: codex`, and append the feedback to Notes.
- Blocked: retry or blocker re-check request. Keep the card in `Blocked`, set `assignee: codex`, and append the new condition to Notes.

The Task Board runner specification is the operational source for lane behavior:

```text
Projects/codex-task-board-runner/spec.md
```

## Failure Handling

If the helper refuses a change, report the exact command and error. Common causes are missing ticket files, malformed frontmatter delimiters, missing board lanes, or multiple board cards for one ticket. Prefer `show` and `list --json` to inspect state before retrying.
