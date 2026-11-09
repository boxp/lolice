import http from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
const dir = mkdtempSync(join(tmpdir(), 'mcp-test-'));
const pk = 'synthetic-public-key', sk = 'synthetic-secret-key';
writeFileSync(join(dir, 'LANGFUSE_PUBLIC_KEY'), pk); writeFileSync(join(dir, 'LANGFUSE_SECRET_KEY'), sk);
let calls = 0, proxy, logs = '';
const names = ['listObservations','getObservation','getObservationFieldSchema','getObservationFilterSchema','getObservationFilterValues','getHealth'];
const server = http.createServer((req,res) => {
  let body = ''; req.on('data', c => body += c); req.on('end', () => {
    try {
      assert.equal(req.headers.host, 'langfuse.b0xp.io'); assert.equal(req.headers.authorization, `Basic ${Buffer.from(`${pk}:${sk}`).toString('base64')}`); assert.equal(req.headers.cookie, undefined); calls++;
      const rpc = JSON.parse(body); let result = {};
      if (rpc.method === 'initialize') { assert.deepEqual(rpc.params.capabilities, {}); result = {serverInfo:{name:'fake'},capabilities:{tools:{},resources:{},prompts:{}}}; }
      if (rpc.method === 'tools/list') result = {tools: [...names.map(name => ({name,annotations:{readOnlyHint:true}})), {name:'createScore',annotations:{readOnlyHint:false}}]};
      if (rpc.method === 'tools/call') { assert.equal(rpc.params.arguments.limit, 50); result = {content:[]}; }
      res.writeHead(200, {'content-type':'text/event-stream'}); res.end(`data: ${JSON.stringify({jsonrpc:'2.0',id:rpc.id,result})}\n\n`);
    } catch { res.writeHead(500); res.end(); }
  });
});
await new Promise(r => server.listen(0,'127.0.0.1',r));
proxy = spawn(process.execPath,[process.env.TEST_PROXY],{env:{...process.env,LANGFUSE_SECRET_DIR:dir,LANGFUSE_WEB_URL:`http://127.0.0.1:${server.address().port}`}});
proxy.stdout.on('data', c => logs += c); proxy.stderr.on('data', c => logs += c);
try {
  let ready = false; for (let i=0;i<100;i++) { try { if ((await fetch('http://127.0.0.1:3002/healthz')).ok) { ready=true; break; } } catch {} await new Promise(r=>setTimeout(r,50)); } assert.ok(ready,'proxy starts');
  const post = async (body, headers={}) => { const r=await fetch('http://127.0.0.1:3002/mcp',{method:'POST',headers:{'content-type':'application/json',...headers},body:typeof body==='string'?body:JSON.stringify(body)}); const text=await r.text(); assert.ok(!text.includes(pk) && !text.includes(sk)); return {status:r.status, body:JSON.parse(text)}; };
  const rpc=(method,params={})=>({jsonrpc:'2.0',id:1,method,params});
  let r=await post(rpc('initialize',{capabilities:{sampling:{},elicitation:{},roots:{}}}),{authorization:'Basic bogus',cookie:'bad'}); assert.equal(r.body.result.serverInfo.name,'fake'); assert.deepEqual(r.body.result.capabilities,{tools:{}});
  r=await post(rpc('tools/list')); assert.deepEqual(r.body.result.tools.map(t=>t.name),names);
  const before=calls; r=await post(rpc('tools/call',{name:'createScore'})); assert.equal(r.body.error.code,-32602);
  for (const method of ['prompts/list','resources/list']) { r=await post(rpc(method)); assert.equal(r.body.error.code,-32601); } assert.equal(calls,before);
  r=await post(rpc('tools/call',{name:'listObservations',arguments:{limit:100}})); assert.ok(r.body.result);
  r=await post('[]'); assert.equal(r.body.error.code,-32600);
  const beforeBad=calls;
  for (const args of ['str', 1, null, [1]]) { r=await post(rpc('tools/call',{name:'listObservations',arguments:args})); assert.equal(r.body.error.code,-32602); }
  for (const params of ['str', 1, [1]]) { r=await post({jsonrpc:'2.0',id:1,method:'tools/call',params}); assert.equal(r.body.error.code,-32602); }
  assert.equal(calls,beforeBad); assert.ok((await fetch('http://127.0.0.1:3002/healthz')).ok,'proxy survives malformed arguments');
  r=await post(rpc('tools/call',{name:'listObservations',arguments:{limit:'100'}})); assert.ok(r.body.result);
  r=await post(' '.repeat(65537)); assert.equal(r.status,413);
  await new Promise(r=>setTimeout(r,50)); assert.match(logs,/decision=denied/); assert.ok(!logs.includes(pk) && !logs.includes(sk));
  console.log('PASS: MCP proxy unit checks');
} finally { proxy.kill(); server.close(); rmSync(dir,{recursive:true,force:true}); }
