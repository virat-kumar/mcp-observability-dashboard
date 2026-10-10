"""Generate six-MCP Prometheus dashboard using core Grafana visualizations."""
import json
from pathlib import Path
UID='mcp-prometheus'
P=[]
def panel(title,expr,typ='timeseries',unit='short',w=12,h=8,legend='{{mcp}}',desc=''):
    i=len(P)+1
    x=0 if i%2 else 12
    y=((i-1)//2)*8
    field={'defaults':{'unit':unit},'overrides':[]}
    if unit=='ms': field['defaults']['unit']='ms'
    P.append({'id':i,'title':title,'description':desc,'type':typ,'datasource':{'type':'prometheus','uid':UID},'gridPos':{'x':x,'y':y,'w':w,'h':h},'targets':[{'expr':expr,'legendFormat':legend,'refId':'A'}],'fieldConfig':field,'options':{'legend':{'displayMode':'list','placement':'bottom'},'tooltip':{'mode':'multi'}}})
    return i
base='command_end_to_end_latency_milliseconds'
filt='latency_type="enqueue_to_response",request_method="tools/call",mcp=~"$mcp"'
panel('MCP tunnel health','up{job="mcp-tunnels",mcp=~"$mcp"}','timeseries','short',desc='1=healthy scrape; 0=unavailable')
panel('Request volume (5m)',f'sum by(mcp)(increase({base}_count{{{filt}}}[5m]))')
panel('Average end-to-end latency',f'sum by(mcp)(rate({base}_sum{{{filt}}}[5m])) / clamp_min(sum by(mcp)(rate({base}_count{{{filt}}}[5m])), 1e-9)','timeseries','ms')
panel('P50 end-to-end latency',f'histogram_quantile(0.50, sum by(le,mcp)(rate({base}_bucket{{{filt}}}[5m])))','timeseries','ms')
panel('P95 end-to-end latency',f'histogram_quantile(0.95, sum by(le,mcp)(rate({base}_bucket{{{filt}}}[5m])))','timeseries','ms')
panel('P99 end-to-end latency',f'histogram_quantile(0.99, sum by(le,mcp)(rate({base}_bucket{{{filt}}}[5m])))','timeseries','ms')
panel('Queued commands','sum by(mcp)(commands_queue_length{job="mcp-tunnels",mcp=~"$mcp"})')
panel('Worker occupancy','sum by(mcp)(dispatcher_worker_pool_occupancy{job="mcp-tunnels",mcp=~"$mcp"})')
panel('Tunnel CPU usage (%)','100 * sum by(mcp)(rate(process_cpu_seconds_total{job="mcp-tunnels",mcp=~"$mcp"}[5m]))','timeseries','percent')
panel('Tunnel memory usage','sum by(mcp)(process_resident_memory_bytes{job="mcp-tunnels",mcp=~"$mcp"})','timeseries','bytes')
panel('Poll error rate','sum by(mcp)(rate(commands_poll_errors_total{job="mcp-tunnels",mcp=~"$mcp"}[5m]))')
panel('Request latency histogram count',f'sum by(mcp)({base}_count{{{filt}}})')
dashboard={'uid':'mcp-observatory-v2','title':'MCP Observatory - Grafana','description':'Independent Prometheus analytics for MCP tunnels. Request payload drilldown and telemetry are available in the companion MCP Observatory UI.','schemaVersion':39,'version':1,'refresh':'30s','timezone':'browser','time':{'from':'now-6h','to':'now'},'tags':['MCP','observability','tunnels'],'editable':False,'panels':P,'templating':{'list':[{'name':'mcp','label':'MCP','type':'query','datasource':{'type':'prometheus','uid':UID},'query':{'query':'label_values(up{job="mcp-tunnels"}, mcp)','refId':'A'},'includeAll':True,'allValue':'.*','multi':True,'refresh':1,'current':{'text':'All','value':'$__all'}}]},'annotations':{'list':[]}}
path=Path(__file__).resolve().parents[1]/'grafana/dashboards/mcp-observatory.json'
path.write_text(json.dumps(dashboard,indent=2)+'\n')
print('Grafana dashboard:', len(P), 'panels ->',path)
