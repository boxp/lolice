// BOXP-428: Data Retention is an Enterprise feature, so this uses the OSS public delete API.
// Prints counts only, never bodies. Each run is bounded; the next hourly run continues.
//
// Readback on v4.75.0 showed why each step exists:
// - DELETE /api/public/traces removes rows from ClickHouse but not the raw event objects.
//   Those objects (events/) only expire through the bucket lifecycle rule set here.
// - Media upload stays off (no LANGFUSE_S3_MEDIA_UPLOAD_BUCKET), so no media objects exist.
//   If it is ever enabled, trace deletion removes media no other trace references; do not
//   add an age-based rule for it, because one object can be shared by traces of any age.
// - ClickHouse keeps deleted rows on disk until parts merge, so the daily OPTIMIZE makes
//   deletions physical.
import { createRequire } from 'node:module';

const require = createRequire('/app/worker/');
const { S3Client, PutBucketLifecycleConfigurationCommand, GetBucketLifecycleConfigurationCommand } = require('@aws-sdk/client-s3');

const DAYS = Number(process.env.RETENTION_DAYS || 14);
const EVENT_DAYS = Number(process.env.RAW_EVENT_DAYS || 1);
const BASE = process.env.LANGFUSE_URL || 'http://langfuse-web:3000';
const BUCKET = 'langfuse';
const headers = {
  Authorization: 'Basic ' + Buffer.from(`${process.env.LANGFUSE_PUBLIC_KEY}:${process.env.LANGFUSE_SECRET_KEY}`).toString('base64'),
  'Content-Type': 'application/json',
};
const SCAN_LIMIT = Number(process.env.RETENTION_SCAN_LIMIT || 5000);
const CLICKHOUSE = process.env.CLICKHOUSE_URL || 'http://langfuse-clickhouse:8123';
const chAuth = 'Basic ' + Buffer.from(`langfuse:${process.env.CLICKHOUSE_PASSWORD}`).toString('base64');
const report = { retention_days: DAYS, raw_event_days: EVENT_DAYS };

async function call(path, init = {}) {
  const res = await fetch(BASE + path, { ...init, headers, signal: AbortSignal.timeout(60_000) });
  if (!res.ok) throw new Error(`${init.method || 'GET'} ${path.split('?')[0]} -> ${res.status}`);
  return res.json();
}

async function chQuery(sql, timeoutMs = 120_000) {
  const res = await fetch(CLICKHOUSE, { method: 'POST', headers: { Authorization: chAuth }, body: sql, signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`clickhouse -> ${res.status}`);
  return res.text();
}

// Reading ClickHouse scans the whole table on every run. Deleted traces leave the result, so a
// capped run is continued by the next one and no cursor has to be stored.
async function findTraceIds(where) {
  const body = await chQuery(`SELECT DISTINCT trace_id FROM events_core FINAL WHERE ${where} LIMIT ${SCAN_LIMIT} FORMAT JSONEachRow`);
  return body.split('\n').filter(Boolean).map((line) => JSON.parse(line).trace_id);
}

async function deleteTraces() {
  const expired = await findTraceIds(`start_time < now64(6) - toIntervalDay(${DAYS})`);
  const shared = await findTraceIds('public');
  const ids = [...new Set([...expired, ...shared])];
  for (let i = 0; i < ids.length; i += 1000) {
    await call('/api/public/traces', { method: 'DELETE', body: JSON.stringify({ traceIds: ids.slice(i, i + 1000) }) });
  }
  Object.assign(report, {
    expired_traces_deleted: expired.length,
    public_traces_deleted: shared.length,
    scan_limit_reached: expired.length === SCAN_LIMIT || shared.length === SCAN_LIMIT,
  });
}

async function enforceLifecycle() {
  const s3 = new S3Client({
    region: 'auto',
    endpoint: process.env.S3_ENDPOINT || 'http://langfuse-minio:9000',
    forcePathStyle: true,
    credentials: { accessKeyId: 'minio', secretAccessKey: process.env.MINIO_ROOT_PASSWORD },
  });
  const rule = (prefix, days) => ({ ID: `expire-${prefix.replace('/', '')}`, Status: 'Enabled', Filter: { Prefix: prefix }, Expiration: { Days: days } });
  await s3.send(new PutBucketLifecycleConfigurationCommand({ Bucket: BUCKET, LifecycleConfiguration: { Rules: [rule('events/', EVENT_DAYS)] } }));
  const got = await s3.send(new GetBucketLifecycleConfigurationCommand({ Bucket: BUCKET }));
  report.lifecycle = (got.Rules || []).map((r) => `${r.Filter?.Prefix}:${r.Expiration?.Days}d`);
}

async function optimize() {
  for (const table of ['events_full', 'events_core']) await chQuery(`OPTIMIZE TABLE ${table} FINAL`, 600_000);
  report.optimized = true;
}

const steps = [enforceLifecycle, deleteTraces];
if (process.env.FORCE_OPTIMIZE === 'true' || new Date().getUTCHours() === 18) steps.push(optimize);

let failed = false;
for (const step of steps) {
  try {
    await step();
  } catch (e) {
    failed = true;
    report[`${step.name}_error`] = e.message;
  }
}
console.log(JSON.stringify(report));
process.exit(failed ? 1 : 0);
