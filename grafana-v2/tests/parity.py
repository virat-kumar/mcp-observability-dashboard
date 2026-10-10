"""Compare independent old and new API semantics without writing to either."""
import urllib.request, urllib.parse, json, math, sys
OLD='http://127.0.0.1:9111'; NEW='http://127.0.0.1:9120'; checks=[]

def get(port,route,query=None):
    url=port+route+('?' + urllib.parse.urlencode(query) if query else '')
    with urllib.request.urlopen(url,timeout=20) as response:
        return json.load(response)

def check(name, condition):
    if not condition: raise AssertionError(name)
    checks.append(name)

fixed={'from':'2026-10-01T00:00:00Z','to':'2026-10-09T21:00:00Z','mcp':'all'}
for k in ('total_requests','errors','success_rate','avg_ms','p50_ms','p95_ms','p99_ms','max_ms'):
    a=get(OLD,'/api/summary',fixed)[k];b=get(NEW,'/api/summary',fixed)[k]
    check('summary '+k,a==b or (a is not None and b is not None and math.isclose(a,b,rel_tol=.001)))
a=get(OLD,'/api/timeseries',fixed);b=get(NEW,'/api/timeseries',fixed)
check('timeseries parity',a==b)
for sort in ('newest','oldest','slowest','fastest','reply_delay'):
    q={**fixed,'sort':sort,'limit':20}
    a=get(OLD,'/api/requests',q)['rows'];b=get(NEW,'/api/requests',q)['rows']
    check('request ordering '+sort,[(x['request_key'],x['duration_ms'],x['payload_available']) for x in a]==[(x['request_key'],x['duration_ms'],x['payload_available']) for x in b])
q={**fixed,'sort':'newest','limit':100}
rows=get(OLD,'/api/requests',q)['rows']
item=next((r for r in rows if r['payload_available']==1),None)
check('historical payload available',item is not None)
if item:
    ref=get(OLD,f"/api/request/{item['mcp']}/{item['request_id']}")
    other=get(NEW,f"/api/request/{item['mcp']}/{item['request_id']}")
    check('exact tool request input equality',ref['input_json']==other['input_json'])
    check('exact tool response output equality',ref['output_json']==other['output_json'])
for mcp in ('terminal','playwright','computer','personal','excel','background'):
    q={**fixed,'mcp':mcp,'limit':50}
    a=get(OLD,'/api/telemetry',q)['rows'];b=get(NEW,'/api/telemetry',q)['rows']
    check('telemetry '+mcp,[(r['mcp'],r['seq'],r['client_instance_id']) for r in a]==[(r['mcp'],r['seq'],r['client_instance_id']) for r in b])
check('6 MCP health rows',len(get(NEW,'/api/health')['rows'])==6)
check('4 capture proxy mapping disabled during staging',get(NEW,'/api/config')['http_capture']=={})
print('PASS',len(checks),'feature/API parity checks');print('\n'.join(checks))
