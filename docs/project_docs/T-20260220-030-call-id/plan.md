# Fix: OpenAI Codex `call_id` string too long error

## Task ID

T-20260220-030-call-id

## Problem

When switching to `openai-codex/gpt-5.3-codex`, the API returns:

```
Invalid 'input[2].call_id': string too long. Expected a string with maximum length 64,
but got a string with length 452 instead.
```

This is a reoccurrence of GitHub issue #10640.

## Root Cause

Three issues combine to allow long tool call IDs through to the OpenAI Codex API:

1. **`transcript-policy.ts`**: OpenAI providers had `sanitizeToolCallIds: false`, so `sanitizeToolCallIdsForCloudCodeAssist()` was never called for OpenAI models.
2. **`pi-embedded-helpers/images.ts`**: The `sanitizeToolCallIds` check was gated behind `allowNonImageSanitization` (which is `false` for OpenAI's `"images-only"` sanitize mode), creating a second barrier.
3. **pi-ai library (`openai-responses-shared.js`)**: The `normalizeToolCallId` function skips IDs without `|` separators, and only applies to `!isSameModel` assistant messages.

In cross-model conversations (e.g., Gemini/Anthropic -> OpenAI Codex), tool call IDs from previous providers (which can be 400+ chars) pass through all three layers without being truncated.

## Solution (Applied in openclaw/openclaw)

### 1. New `"openai"` tool call ID mode (`tool-call-id.ts`)

- Added `"openai"` to `ToolCallIdMode` type
- Allows `[a-zA-Z0-9_-]` characters (matching OpenAI's requirements)
- Enforces max 64 chars **total** (including pipe separators)
- Preserves `|` separator for `call_id|item_id` format
- Handles collision deduplication with hash suffixes

### 2. Enable sanitization for OpenAI providers (`transcript-policy.ts`)

- Added `isOpenAi` to `sanitizeToolCallIds` condition
- Set `toolCallIdMode: "openai"` for OpenAI providers
- Removed `!isOpenAi` guard from `sanitizeToolCallIds` return field

### 3. Decouple tool call ID sanitization from sanitize mode (`images.ts`)

- Removed `allowNonImageSanitization` dependency from `sanitizeToolCallIds` check
- Tool call ID sanitization now runs regardless of image sanitization mode

## Files Changed (openclaw/openclaw repo)

- `src/agents/tool-call-id.ts` - New openai mode implementation
- `src/agents/transcript-policy.ts` - Policy updates for OpenAI
- `src/agents/pi-embedded-helpers/images.ts` - Gate removal
- `src/agents/tool-call-id.e2e.test.ts` - 8 new openai mode e2e tests
- `src/agents/transcript-policy.test.ts` - Updated expectations
- `src/agents/transcript-policy.e2e.test.ts` - Updated expectations
- `src/agents/pi-embedded-runner.sanitize-session-history.test.ts` - Updated expectations
- `src/agents/pi-embedded-runner.sanitize-session-history.e2e.test.ts` - Updated expectations

## Test Results

- `pnpm vitest run --config vitest.unit.config.ts` - 15 unit tests pass (transcript-policy + sanitize-session-history)
- `pnpm vitest run --config vitest.e2e.config.ts src/agents/tool-call-id.e2e.test.ts` - 15 e2e tests pass (8 new openai mode tests)

## Codex Review

- First review identified pipe-separated IDs could exceed 64-char total limit -> Fixed
- Second review: No issues found

## Worktree Location

Changes are available in the local openclaw worktree:

```
/home/node/worktrees/github.com/openclaw/openclaw/fix-codex-tool-call-id
```

Branch: `fix/codex-tool-call-id` (3 commits ahead of origin/main)

## Deployment Notes

Once the openclaw code changes are merged and a new image is built, the fix will be automatically deployed to the lolice cluster via ArgoCD Image Updater.
