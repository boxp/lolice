import datetime, json, os, sys, urllib.request, urllib.error
proxy, web, trace = sys.argv[1:]
def request(path, body=None, headers=None, host=proxy, port=3002):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(f'http://{host}:{port}{path}', data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            assert all(os.environ[k].encode() not in body for k in ('LF_PK','LF_SK')), 'key leaked in response'
            return r.status, body
    except urllib.error.HTTPError as e: return e.code, e.read()
def rpc(method, params=None):
    status, body = request('/mcp', {'jsonrpc':'2.0','id':1,'method':method,'params':params or {}}, {'Authorization':'Basic bogus','Content-Type':'application/json'})
    assert status == 200
    return json.loads(body)
assert request('/healthz') == (200,b'ok')
r = rpc('initialize', {'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'chainsaw','version':'1'}})
assert 'result' in r, f"initialize failed: {r.get('error')}"
assert r['result']['serverInfo'] and not ({'resources','prompts'} & r['result']['capabilities'].keys())
r = rpc('tools/list'); names = {t['name'] for t in r['result']['tools']}
assert names == {'listObservations','getObservation','getObservationFieldSchema','getObservationFilterSchema','getObservationFilterValues','getHealth'}
assert not names & {'createTextPrompt','deleteDatasetItem','submitFeedback','createScore'}
assert rpc('tools/call', {'name':'createScore','arguments':{}})['error']['code'] == -32602
for method in ('prompts/list','resources/list'): assert rpc(method)['error']['code'] == -32601
now = datetime.datetime.now(datetime.timezone.utc)
start = (now - datetime.timedelta(minutes=59)).isoformat(); end = (now + datetime.timedelta(minutes=1)).isoformat()
args = {'traceId':trace,'fromStartTime':start,'toStartTime':end,'fields':['id','traceId']}
r = rpc('tools/call', {'name':'listObservations','arguments':args})
def objects(x):
    if isinstance(x,dict):
        yield x
        for v in x.values(): yield from objects(v)
    elif isinstance(x,list):
        for v in x: yield from objects(v)
    elif isinstance(x,str):
        try: yield from objects(json.loads(x))
        except (ValueError,RecursionError): pass
matches = [v for v in objects(r) if v.get('traceId') == trace and v.get('id')]
assert matches, 'ingested observation missing'
args.update(observationId=matches[0]['id'], fields=['id','traceId','input','output'])
r = rpc('tools/call', {'name':'getObservation','arguments':args})
assert 'chainsaw-body-marker' in json.dumps(r), 'observation body missing'
status, _ = request('/api/public/mcp', {'jsonrpc':'2.0','id':1,'method':'tools/list'}, {'Host':'langfuse-web.langfuse.svc.cluster.local:3000','Content-Type':'application/json'}, web, 3000)
assert status in (401,403)
assert request('/mcp', b' '*65537)[0] == 413
assert json.loads(request('/mcp', b'[]')[1])['error']['code'] == -32600
print('PASS: real Langfuse MCP proxy checks')
