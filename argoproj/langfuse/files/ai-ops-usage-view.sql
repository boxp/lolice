-- BOXP-472: the only object the Grafana user can read (clickhouse-users.xml). It runs as the
-- definer, so that user holds no grant on events_core and cannot select input/output/metadata.
--
-- Limited to the ai-ops project (LANGFUSE_INIT_PROJECT_ID in web.yaml), so another project's usage
-- never appears in the AI Ops totals and a same-id trace in it cannot supply a parent AGENT name.
--
-- Counts GENERATION rows only. Traces and parent AGENT/SPAN rows carry no usage of their own
-- that may be added on top, so a parent and its children are never counted twice.
-- start_time is the call's own time from the session log or hook, not the export time.
--
-- Usage buckets are exclusive in both producers (Task Board exporter: codex-usage/claude-usage,
-- Hermes observer: _usage_details), so usage total equals their sum. Key names differ by source
-- and are normalized here. other_tokens exposes any bucket this view does not know about.
CREATE OR REPLACE VIEW default.ai_ops_usage_v1
DEFINER = langfuse SQL SECURITY DEFINER
AS
SELECT
    g.start_time AS start_time,
    g.trace_id AS trace_id,
    g.span_id AS observation_id,
    g.trace_name AS trace_name,
    if(g.provided_model_name = '', '(モデル未記録)', g.provided_model_name) AS model,
    multiIf(
        startsWith(g.provided_model_name, 'claude'), 'claude',
        startsWith(g.provided_model_name, 'gpt'), 'codex',
        startsWith(g.provided_model_name, 'gemini'), 'gemini',
        g.provided_model_name = '', '(不明)',
        'その他'
    ) AS provider,
    multiIf(
        startsWith(g.trace_name, 'task-board-run ') AND a.name = '', 'Task Board / 親 (CLI)',
        startsWith(g.trace_name, 'task-board-run ') AND a.name LIKE 'dele%:codex', 'Task Board / 子委譲 (Codex)',
        startsWith(g.trace_name, 'task-board-run ') AND a.name = 'review-gate', 'Task Board / review gate',
        startsWith(g.trace_name, 'task-board-run ') AND a.name = 'codex-session', 'Task Board / 親 (Codex session)',
        startsWith(g.trace_name, 'task-board-run ') AND a.name LIKE 'subagent%', 'Task Board / subagent',
        startsWith(g.trace_name, 'task-board-run '), 'Task Board / その他 agent',
        a.name LIKE 'subagent%', 'Hermes / 子 agent',
        has(g.tags, 'g2'), 'Hermes / G2',
        has(g.tags, 'platform:telegram'), 'Hermes / Telegram 会話',
        has(g.tags, 'platform:cron'), 'Hermes / cron',
        has(g.tags, 'platform:webhook'), 'Hermes / Webhook',
        has(g.tags, 'platform:api_server'), 'Hermes / API',
        has(g.tags, 'hermes'), 'Hermes / platform 不明',
        '(未分類)'
    ) AS route,
    -- Hermes turns can name several tickets in free text, so they are not split across tickets.
    if(startsWith(g.trace_name, 'task-board-run '), substring(g.trace_name, 16), '') AS ticket,
    g.usage_details['input'] AS input_tokens,
    g.usage_details['input_cached_tokens'] + g.usage_details['cache_read_input_tokens'] AS cache_read_tokens,
    g.usage_details['input_cache_creation'] + g.usage_details['input_cache_write_tokens']
        + g.usage_details['cache_creation_input_tokens'] AS cache_write_tokens,
    g.usage_details['output'] AS output_tokens,
    g.usage_details['output_reasoning_tokens'] AS reasoning_tokens,
    g.usage_details['total'] AS total_tokens,
    toInt64(g.usage_details['total']) - toInt64(g.usage_details['input'] + g.usage_details['input_cached_tokens']
        + g.usage_details['cache_read_input_tokens'] + g.usage_details['input_cache_creation']
        + g.usage_details['input_cache_write_tokens'] + g.usage_details['cache_creation_input_tokens']
        + g.usage_details['output'] + g.usage_details['output_reasoning_tokens']) AS other_tokens,
    mapContains(g.usage_details, 'total') AS has_usage
FROM
(
    SELECT trace_id, span_id, parent_span_id, start_time, trace_name, tags, provided_model_name, usage_details
    FROM default.events_core FINAL
    WHERE project_id = 'ai-ops' AND type = 'GENERATION'
) AS g
LEFT JOIN
(
    SELECT trace_id, span_id, any(name) AS name
    FROM default.events_core FINAL
    WHERE project_id = 'ai-ops' AND type = 'AGENT'
    GROUP BY trace_id, span_id
) AS a
ON g.trace_id = a.trace_id AND g.parent_span_id = a.span_id
SETTINGS join_use_nulls = 0;
