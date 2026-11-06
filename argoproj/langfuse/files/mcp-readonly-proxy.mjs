import http from 'node:http';
import { readFile } from 'node:fs/promises';

const allowed = new Set((process.env.LANGFUSE_MCP_ALLOWED_TOOLS ?? 'listObservations,getObservation,getObservationFieldSchema,getObservationFilterSchema,getObservationFilterValues,getHealth').split(',').map(s => s.trim()).filter(Boolean));
const methods = new Set(['initialize', 'notifications/initialized', 'ping', 'tools/list', 'tools/call']);
const upstream = new URL('/api/public/mcp', process.env.LANGFUSE_WEB_URL ?? 'http://langfuse-web:3000');
const publicHost = new URL(`http://${process.env.LANGFUSE_PUBLIC_HOST ?? 'langfuse.b0xp.io'}`).host;
const secretDir = process.env.LANGFUSE_SECRET_DIR ?? '/var/run/secrets/langfuse';
const maxRequest = 64 * 1024, maxResponse = 512 * 1024;
const error = (id, code, message) => ({ jsonrpc: '2.0', id: id ?? null, error: { code, message } });

http.createServer((req, res) => handle(req, res).catch(() => { if (!res.headersSent) { res.writeHead(500, { 'content-type': 'text/plain' }); res.end('internal error'); } else res.destroy(); })).listen(3002, '0.0.0.0');

async function handle(req, res) {
  const start = Date.now(); let method = '-', tool = '-', decision = 'denied', status = '-', bytes = 0, id = null;
  const send = (statusCode, value) => {
    const body = value === null ? '' : typeof value === 'string' ? value : JSON.stringify(value);
    bytes = Buffer.byteLength(body); res.writeHead(statusCode, { 'content-type': typeof value === 'string' ? 'text/plain' : 'application/json' }); res.end(body);
  };
  res.on('finish', () => console.log(`${new Date().toISOString()} method=${method} tool=${tool} decision=${decision} upstream=${status} bytes=${bytes} duration_ms=${Date.now() - start}`));
  if (req.url === '/healthz' && req.method === 'GET') { decision = 'allowed'; return send(200, 'ok'); }
  if (req.url !== '/mcp') return send(404, 'not found');
  if (req.method !== 'POST') return send(405, 'method not allowed');
  let chunks = [], size = 0, oversized = false;
  for await (const chunk of req) { size += chunk.length; if (size > maxRequest) { oversized = true; chunks = []; } else if (!oversized) chunks.push(chunk); }
  if (oversized) return send(413, error(null, -32600, 'request too large'));
  let rpc;
  try { rpc = JSON.parse(Buffer.concat(chunks).toString()); } catch { return send(400, error(null, -32700, 'parse error')); }
  if (!rpc || Array.isArray(rpc) || typeof rpc !== 'object' || rpc.jsonrpc !== '2.0' || typeof rpc.method !== 'string' || ('id' in rpc && !['string', 'number'].includes(typeof rpc.id) && rpc.id !== null)) return send(400, error(null, -32600, 'invalid request'));
  id = rpc.id ?? null;
  // Do not log arbitrary client-controlled strings.
  method = methods.has(rpc.method) ? rpc.method : 'other';
  const notification = !Object.hasOwn(rpc, 'id');
  if (!methods.has(rpc.method)) return notification ? send(202, null) : send(200, error(id, -32601, 'method not found'));
  const isPlainObject = v => v !== null && typeof v === 'object' && !Array.isArray(v);
  if ('params' in rpc && rpc.params !== undefined && !isPlainObject(rpc.params)) return notification ? send(202, null) : send(200, error(id, -32602, 'invalid params'));
  if (rpc.method === 'tools/call') {
    const name = rpc.params?.name;
    tool = allowed.has(name) ? name : 'other';
    if (!allowed.has(name)) return notification ? send(202, null) : send(200, error(id, -32602, 'tool not allowed by read-only proxy'));
    if (rpc.params.arguments !== undefined && !isPlainObject(rpc.params.arguments)) return notification ? send(202, null) : send(200, error(id, -32602, 'tool arguments must be an object'));
    if (name === 'listObservations') {
      const args = { ...(rpc.params.arguments ?? {}) };
      if (typeof args.limit !== 'number' || !Number.isFinite(args.limit) || args.limit < 1 || args.limit > 50) args.limit = 50;
      rpc.params.arguments = args;
    }
  }
  if (rpc.method === 'initialize' && rpc.params?.capabilities && typeof rpc.params.capabilities === 'object') {
    delete rpc.params.capabilities.sampling; delete rpc.params.capabilities.elicitation; delete rpc.params.capabilities.roots;
  }
  decision = 'allowed';
  try {
    const [pk, sk] = await Promise.all(['LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'].map(n => readFile(`${secretDir}/${n}`, 'utf8')));
    const headers = { host: publicHost, authorization: `Basic ${Buffer.from(`${pk.trim()}:${sk.trim()}`).toString('base64')}`, 'content-type': 'application/json', accept: 'application/json, text/event-stream' };
    for (const h of ['mcp-protocol-version', 'mcp-session-id']) if (req.headers[h]) headers[h] = req.headers[h];
    const result = await new Promise((resolve, reject) => {
      const request = http.request(upstream, { method: 'POST', headers, timeout: 30000 }, response => {
        status = response.statusCode; let parts = [], total = 0;
        response.on('data', chunk => { total += chunk.length; if (total > maxResponse) { response.destroy(); resolve({ tooLarge: true }); } else parts.push(chunk); });
        response.on('end', () => resolve({ body: Buffer.concat(parts).toString(), type: response.headers['content-type'] ?? '' }));
        response.on('error', reject);
      });
      request.on('timeout', () => request.destroy(new Error('timeout'))); request.on('error', reject); request.end(JSON.stringify(rpc));
    });
    if (status < 200 || status >= 300) return notification ? send(202, null) : send(200, error(id, -32000, status === 401 || status === 403 ? 'upstream authentication failed' : `upstream HTTP ${status}`));
    if (result.tooLarge) return send(200, error(id, -32000, 'response too large; request fewer fields/limit'));
    if (notification && !result.body) return send(202, null);
    const body = result.type.includes('text/event-stream') ? result.body.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n') : result.body;
    const value = JSON.parse(body);
    if (rpc.method === 'tools/list' && Array.isArray(value.result?.tools)) value.result.tools = value.result.tools.filter(t => allowed.has(t.name) && t.annotations?.readOnlyHint === true);
    if (rpc.method === 'initialize' && value.result?.capabilities) { delete value.result.capabilities.prompts; delete value.result.capabilities.resources; }
    return send(200, value);
  } catch { return notification ? send(202, null) : send(200, error(id, -32000, 'upstream request failed')); }
}
