// BOXP-428: Data Retention is an Enterprise feature, so this uses the OSS public API.
// Prints counts only, never bodies. Pages are bounded; the next hourly run continues.
//
// Readback on v4.75.0 showed why each step exists:
// - DELETE /api/public/traces removes rows from ClickHouse but not the raw event objects.
//   Those objects (events/) only expire through the bucket lifecycle rule set here.
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
const MAX_PAGES = 50;
const report = { retention_days: DAYS, raw_event_days: EVENT_DAYS };

async function call(path, init = {}) {
  const res = await fetch(BASE + path, { ...init, headers, signal: AbortSignal.timeout(60_000) });
  if (!res.ok) throw new Error(`${init.method || 'GET'} ${path.split('?')[0]} -> ${res.status}`);
  return res.json();
}

async function listTraceIds(query, keep = () => true) {
  const ids = new Set();
  let cursor;
  for (let page = 0; page < MAX_PAGES; page++) {
    const suffix = cursor ? `&cursor=${encodeURIComponent(cursor)}` : '';
    const body = await call(`/api/public/v2/observations?limit=1000&fields=core${query}${suffix}`);
    for (const o of body.data) if (keep(o)) ids.add(o.traceId);
    cursor = body.meta?.cursor;
    if (!cursor) break;
  }
  return [...ids];
}

async function deleteTraces() {
  const cutoff = new Date(Date.now() - DAYS * 86_400_000).toISOString();
  const expired = await listTraceIds(`&toStartTime=${encodeURIComponent(cutoff)}`);
  const shared = await listTraceIds('', (o) => o.public === true);
  const ids = [...new Set([...expired, ...shared])];
  for (let i = 0; i < ids.length; i += 1000) {
    await call('/api/public/traces', { method: 'DELETE', body: JSON.stringify({ traceIds: ids.slice(i, i + 1000) }) });
  }
  Object.assign(report, { cutoff, expired_traces_deleted: expired.length, public_traces_deleted: shared.length });
}

async function enforceLifecycle() {
  const s3 = new S3Client({
    region: 'auto',
    endpoint: process.env.S3_ENDPOINT || 'http://langfuse-minio:9000',
    forcePathStyle: true,
    credentials: { accessKeyId: 'minio', secretAccessKey: process.env.MINIO_ROOT_PASSWORD },
  });
  const rule = (prefix) => ({ ID: `expire-${prefix.replace('/', '')}`, Status: 'Enabled', Filter: { Prefix: prefix }, Expiration: { Days: EVENT_DAYS } });
  await s3.send(new PutBucketLifecycleConfigurationCommand({ Bucket: BUCKET, LifecycleConfiguration: { Rules: [rule('events/'), rule('media/')] } }));
  const got = await s3.send(new GetBucketLifecycleConfigurationCommand({ Bucket: BUCKET }));
  report.lifecycle = (got.Rules || []).map((r) => `${r.Filter?.Prefix}:${r.Expiration?.Days}d`);
}

async function optimize() {
  const auth = 'Basic ' + Buffer.from(`langfuse:${process.env.CLICKHOUSE_PASSWORD}`).toString('base64');
  for (const table of ['events_full', 'events_core']) {
    const res = await fetch(process.env.CLICKHOUSE_URL || 'http://langfuse-clickhouse:8123', {
      method: 'POST', headers: { Authorization: auth }, body: `OPTIMIZE TABLE ${table} FINAL`, signal: AbortSignal.timeout(600_000),
    });
    if (!res.ok) throw new Error(`optimize ${table} -> ${res.status}`);
  }
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
