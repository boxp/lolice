// 実 MinIO へ小さな object を置く。キーは events/otel/ より前に並ぶので、
// 一覧を先頭の数ページで打ち切る実装は実 raw event に到達できない。
const { createRequire } = require('node:module');
const { S3Client, PutObjectCommand } = createRequire('/app/worker/')('@aws-sdk/client-s3');

const COUNT = Number(process.env.SEED_COUNT);
const s3 = new S3Client({ region: 'auto', endpoint: process.env.S3_ENDPOINT || 'http://langfuse-minio:9000',
  forcePathStyle: true, credentials: { accessKeyId: 'minio', secretAccessKey: process.env.MINIO_ROOT_PASSWORD } });

(async () => {
  let next = 0;
  const worker = async () => {
    while (next < COUNT) {
      const i = next++;
      await s3.send(new PutObjectCommand({ Bucket: 'langfuse', Key: `events/aaa-seed/${String(i).padStart(6, '0')}`, Body: 'x' }));
    }
  };
  await Promise.all(Array.from({ length: 32 }, worker));
  console.log(`seeded ${COUNT}`);
})().catch((e) => { console.error(e.message); process.exit(1); });
