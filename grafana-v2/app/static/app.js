const state = {
  config: null,
  from: null,
  to: null,
  mcp: 'all',
  activeTab: 'overview',
  activePreset: 360,
  rawKind: 'status',
  rawMcp: 'terminal',
  refreshing: false,
  telemetryRows: [],
  latencyHitboxes: []
};

const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));
const fmtMs = v => v == null ? '—' : Number(v) < 1000
  ? Number(v).toFixed(Number(v) < 100 ? 1 : 0) + ' ms'
  : (Number(v) / 1000).toFixed(2) + ' s';
const fmtNum = v => v == null ? '—' : Number(v).toLocaleString(undefined, {maximumFractionDigits: 1});
const fmtBytes = v => {
  if (v == null) return '—';
  let n = Number(v), u = ['B','KB','MB','GB','TB'], i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
};
const fmtTime = s => {
  if (!s) return '—';
  const d = new Date(s);
  return isNaN(d) ? esc(s) : d.toLocaleString([], {
    year:'numeric', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit', fractionalSecondDigits:3
  });
};
const ago = s => {
  if (!s) return 'never';
  const x = (Date.now() - new Date(s)) / 1000;
  if (x < 2) return 'just now';
  if (x < 60) return Math.floor(x) + 's ago';
  if (x < 3600) return Math.floor(x / 60) + 'm ago';
  return Math.floor(x / 3600) + 'h ago';
};
const pretty = v => {
  if (v == null) return '—';
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
};

async function api(path, params) {
  const r = await fetch(path + (params ? '?' + params : ''), {cache: 'no-store'});
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}

function localInput(d) {
  const x = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return x.toISOString().slice(0,16);
}

function qparams(extra = {}) {
  const p = new URLSearchParams();
  if (state.from) p.set('from', state.from);
  if (state.to) p.set('to', state.to);
  p.set('mcp', state.mcp);
  Object.entries(extra).forEach(([k,v]) => {
    if (v !== undefined && v !== null && v !== '') p.set(k, v);
  });
  return p;
}

function rangeName(min) {
  return min === 15 ? '15m' : min === 60 ? '1h' : min === 360 ? '6h'
    : min === 1440 ? '24h' : min === 10080 ? '7d' : min + 'm';
}

function applyPreset(min, refreshInputs = true) {
  state.activePreset = min;
  const to = new Date();
  const from = new Date(to.getTime() - min * 60000);
  state.from = from.toISOString();
  state.to = to.toISOString();
  if (refreshInputs) {
    $('#fromInput').value = localInput(from);
    $('#toInput').value = localInput(to);
  }
  $$('.preset').forEach(b => b.classList.toggle('active', Number(b.dataset.min) === min));
  $('#rangeStatus').textContent = rangeName(min) + ' · live window';
}

function applyCustomRange() {
  state.activePreset = null;
  state.from = $('#fromInput').value ? new Date($('#fromInput').value).toISOString() : null;
  state.to = $('#toInput').value ? new Date($('#toInput').value).toISOString() : null;
  $$('.preset').forEach(x => x.classList.remove('active'));
  $('#rangeStatus').textContent = 'custom range';
}

function kpi(label, value, note, cls = '') {
  return '<div class="card kpi"><div class="label">' + esc(label) + '</div>' +
    '<div class="value ' + cls + '">' + value + '</div>' +
    '<div class="note">' + esc(note) + '</div></div>';
}

function requestRows(rows) {
  if (!rows.length) return '<div class="empty">No requests in this time range.</div>';
  let h = '<table><thead><tr>' +
    '<th>MCP</th><th>Tool</th><th>Input / enqueued</th><th>MCP replied</th>' +
    '<th>OpenAI delivered</th><th title="End-to-end">End-to-end (E2E)</th>' +
    '<th>Reply → delivery</th><th>Status</th><th>Timing</th><th>Payload</th><th>Request ID</th>' +
    '</tr></thead><tbody>';
  for (const r of rows) {
    const name = state.config?.tunnels?.[r.mcp]?.name || r.mcp;
    const payload = r.payload_available
      ? '<span class="payload-pill">input + output</span>'
      : '<span class="payload-pill off">not captured</span>';
    h += '<tr class="clickable request-row" data-mcp="' + esc(r.mcp) + '" data-request="' + esc(r.request_id) + '">' +
      '<td>' + esc(name) + '</td>' +
      '<td class="mono">' + esc(r.tool_name || (r.rpc_method === 'tools/call' ? 'tools/call' : r.rpc_method || '—')) + '</td>' +
      '<td>' + fmtTime(r.input_at) + '</td>' +
      '<td>' + fmtTime(r.mcp_reply_at) + '</td>' +
      '<td>' + fmtTime(r.delivered_at) + '</td>' +
      '<td class="' + (r.duration_ms > 5000 ? 'warn' : '') + '">' + fmtMs(r.duration_ms) + '</td>' +
      '<td>' + fmtMs(r.reply_to_delivery_ms) + '</td>' +
      '<td>' + (r.has_error ? '<span class="bad">ERROR</span>' : '<span class="ok">' + esc(r.status_code || 'OK') + '</span>') + '</td>' +
      '<td class="' + (r.timing_confidence === 'exact-single' ? 'ok' : 'warn') + '">' + esc(r.timing_confidence || 'pending') + '</td>' +
      '<td>' + payload + '</td>' +
      '<td class="mono">' + esc(r.request_id) + '</td></tr>';
  }
  return h + '</tbody></table>';
}

function bindRequestRows(root) {
  root.querySelectorAll('.request-row').forEach(tr => {
    tr.addEventListener('click', () => openRequestDetail(tr.dataset.mcp, tr.dataset.request));
  });
}

function prepCanvas(c, height = 285) {
  const d = window.devicePixelRatio || 1;
  const w = Math.max(320, c.clientWidth), h = height;
  c.width = w * d; c.height = h * d;
  const x = c.getContext('2d');
  x.scale(d,d); x.clearRect(0,0,w,h); x.font = '11px system-ui';
  return [x,w,h];
}

function drawTimeline(c, points) {
  const [x,w,h] = prepCanvas(c);
  if (!points.length) {
    x.fillStyle = '#91a4bd'; x.fillText('No request data in this range', 20, 35); return;
  }
  const L=46,R=58,T=18,B=34,W=w-L-R,H=h-T-B;
  const maxCount=Math.max(1,...points.map(a=>a.count));
  const maxLat=Math.max(1,...points.map(a=>a.avg_ms||0),...points.map(a=>a.p95_ms||0));
  x.strokeStyle='#223a59'; x.lineWidth=1;
  for(let i=0;i<5;i++){const y=T+H*i/4;x.beginPath();x.moveTo(L,y);x.lineTo(w-R,y);x.stroke();}
  const bw=Math.max(2,W/points.length*.6);
  points.forEach((a,i)=>{
    const xx=L+(i+.5)*W/points.length;
    const bar=H*a.count/maxCount;
    x.fillStyle='#67a5ff'; x.fillRect(xx-bw/2,T+H-bar,bw,bar);
    if(a.errors){x.fillStyle='#ff6b7a';const eb=H*a.errors/maxCount;x.fillRect(xx-bw/2,T+H-eb,bw,eb);}
  });
  x.strokeStyle='#62d4e7'; x.lineWidth=2; x.beginPath();
  points.forEach((a,i)=>{
    const xx=L+(i+.5)*W/points.length, y=T+H-H*(a.avg_ms||0)/maxLat;
    i ? x.lineTo(xx,y) : x.moveTo(xx,y);
  }); x.stroke();
  x.fillStyle='#91a4bd';
  x.fillText(String(maxCount),5,T+8); x.fillText('0',28,T+H+4);
  x.fillText(fmtMs(maxLat),w-R+5,T+8); x.fillText('0 ms',w-R+7,T+H+4);
  const first=new Date(points[0].ts), last=new Date(points.at(-1).ts);
  x.fillText(first.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),L,h-8);
  const lastText=last.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  x.fillText(lastText,w-R-x.measureText(lastText).width,h-8);
}

function drawLatencyBars(c, rows) {
  const [x,w,h] = prepCanvas(c);
  state.latencyHitboxes = [];
  const all = rows.length ? rows : Object.entries(state.config?.tunnels || {}).map(([mcp,cfg]) => ({
    mcp, name:cfg.name, count:0, avg_ms:null, p95_ms:null
  }));
  const L=142,R=34,T=26,B=48,W=w-L-R,H=h-T-B;
  const max=Math.max(1,...all.map(z=>Math.max(z.p95_ms||0,z.avg_ms||0)));
  const rh=H/Math.max(1,all.length);

  x.strokeStyle='#2a4261'; x.lineWidth=1;
  for(let i=0;i<=4;i++){
    const xx=L+W*i/4;
    x.beginPath();x.moveTo(xx,T);x.lineTo(xx,T+H);x.stroke();
    x.fillStyle='#91a4bd';
    const t=fmtMs(max*i/4);
    x.fillText(t,Math.max(L,xx-x.measureText(t).width/2),T+H+19);
  }
  x.fillStyle='#9fb3cb'; x.font='600 11px system-ui';
  x.fillText('MCP server (Y)',8,15);
  const axis='Latency (X) · Average / P95';
  x.fillText(axis,L+W/2-x.measureText(axis).width/2,h-5);
  x.font='11px system-ui';

  all.forEach((z,i)=>{
    const y=T+i*rh+rh*.17;
    const barH=Math.max(16,rh*.6);
    const labelY=y+barH*.63;
    x.fillStyle=z.count?'#b4c5da':'#6e8199';
    x.fillText(z.name.slice(0,22),8,labelY);
    x.fillStyle='#20344e'; x.fillRect(L,y,W,barH);
    if(z.p95_ms!=null){x.fillStyle='#62d4e7';x.fillRect(L,y,W*(z.p95_ms/max),barH);}
    if(z.avg_ms!=null){x.fillStyle='#67a5ff';x.fillRect(L,y,W*(z.avg_ms/max),barH*.46);}
    const text=z.count ? ('avg '+fmtMs(z.avg_ms)+' · p95 '+fmtMs(z.p95_ms)) : 'no calls in range';
    x.fillStyle=z.count?'#eaf2ff':'#72869f';
    x.fillText(text,L+7,labelY);
    state.latencyHitboxes.push({x:0,y:y-4,w:w,h:barH+8,mcp:z.mcp});
  });

  c.style.cursor='pointer';
}

function switchTab(name) {
  state.activeTab=name;
  $$('.nav button').forEach(x=>x.classList.toggle('active',x.dataset.tab===name));
  $$('.tab').forEach(x=>x.classList.remove('active'));
  $('#tab-'+name).classList.add('active');
}

async function loadOverview() {
  const sort=$('#overviewSort').value;
  const [s,t,reqs] = await Promise.all([
    api('/api/summary',qparams()),
    api('/api/timeseries',qparams()),
    api('/api/requests',qparams({sort,limit:40}))
  ]);
  $('#kpis').innerHTML=[
    kpi('Requests',fmtNum(s.total_requests),'completed'),
    kpi('Success',s.success_rate==null?'—':s.success_rate.toFixed(2)+'%','non-error',s.success_rate!=null&&s.success_rate<99?'warn':'ok'),
    kpi('Average',fmtMs(s.avg_ms),'end-to-end'),
    kpi('P50',fmtMs(s.p50_ms),'median'),
    kpi('P95',fmtMs(s.p95_ms),'tail'),
    kpi('P99',fmtMs(s.p99_ms),'high-tail'),
    kpi('Maximum',fmtMs(s.max_ms),'slowest',s.max_ms>10000?'warn':'')
  ].join('');
  $('#bucketLabel').textContent='bucket '+t.bucket_seconds+'s';
  drawTimeline($('#timeline'),t.points);
  drawLatencyBars($('#mcpLatency'),s.per_mcp);
  $('#overviewTable').innerHTML=requestRows(reqs.rows);
  bindRequestRows($('#overviewTable'));
}

async function loadRequests() {
  const r=await api('/api/requests',qparams({
    sort:$('#requestSort').value,limit:500,errors_only:$('#errorsOnly').checked
  }));
  $('#requestTable').innerHTML=requestRows(r.rows);
  bindRequestRows($('#requestTable'));
}

async function loadHealth() {
  const h=await api('/api/health');
  $('#healthUpdated').textContent='updated '+new Date().toLocaleTimeString();
  $('#healthGrid').innerHTML=h.rows.map(r=>
    '<div class="card"><div class="section"><h2>'+esc(r.name)+'</h2>' +
    '<span class="badge"><i class="dot '+(r.healthy&&r.ready?'ok':r.healthy?'warn':'bad')+'"></i>' +
    (r.healthy&&r.ready?'healthy':r.healthy?'not ready':'down')+'</span></div>' +
    '<div class="kv"><div>Health</div><div class="'+(r.healthy?'ok':'bad')+'">'+(r.healthy?'LIVE':'FAIL')+'</div>' +
    '<div>Ready</div><div class="'+(r.ready?'ok':'warn')+'">'+(r.ready?'READY':'NO')+'</div>' +
    '<div>Last seen</div><div>'+ago(r.last_seen)+'</div>' +
    '<div>Uptime</div><div>'+(r.uptime_seconds==null?'—':(r.uptime_seconds/3600).toFixed(1)+' h')+'</div>' +
    '<div>Tool calls</div><div>'+fmtNum(r.tool_calls_total)+'</div>' +
    '<div>Poll errors</div><div class="'+(r.poll_errors_total?'warn':'')+'">'+fmtNum(r.poll_errors_total)+'</div>' +
    '<div>Queue</div><div>'+fmtNum(r.queue_length)+' / '+fmtNum(r.queue_capacity)+'</div>' +
    '<div>Workers</div><div>'+fmtNum(r.worker_occupancy)+' / '+fmtNum(r.worker_capacity)+'</div>' +
    '<div>RSS</div><div>'+fmtBytes(r.rss_bytes)+'</div>' +
    '<div>Net RX / TX</div><div>'+fmtBytes(r.net_rx_bytes)+' / '+fmtBytes(r.net_tx_bytes)+'</div></div>' +
    (r.error?'<div class="bad" style="margin-top:8px;font-size:10px">'+esc(r.error)+'</div>':'')+'</div>'
  ).join('');
}

async function loadTelemetry() {
  const wrap=$('#telemetryTable');
  const prevLeft=wrap.scrollLeft, prevTop=wrap.scrollTop;
  const r=await api('/api/telemetry',qparams({
    level:$('#levelFilter').value,
    q:$('#telemetrySearch').value,
    order:$('#telemetryOrder').value,
    limit:800
  }));
  state.telemetryRows=r.rows;
  if(!r.rows.length){wrap.innerHTML='<div class="empty">No telemetry events.</div>';return;}
  let h='<table><thead><tr><th>Time</th><th>MCP</th><th>Level</th><th>Event</th><th>Request</th><th>Method</th><th>Status</th><th>Metadata preview</th></tr></thead><tbody>';
  r.rows.forEach((a,i)=>{
    let preview=a.attrs_json||'{}';
    if(preview.length>220)preview=preview.slice(0,220)+'…';
    h+='<tr data-event-index="'+i+'"><td>'+fmtTime(a.ts)+'</td>' +
      '<td>'+esc(state.config?.tunnels?.[a.mcp]?.name||a.mcp)+'</td>' +
      '<td class="'+(a.level==='WARN'?'warn':a.level==='ERROR'?'bad':'muted')+'">'+esc(a.level)+'</td>' +
      '<td>'+esc(a.message)+'</td><td class="mono">'+esc(a.request_id||'—')+'</td>' +
      '<td class="mono">'+esc(a.rpc_method||'—')+'</td><td>'+esc(a.status_code??'—')+'</td>' +
      '<td class="mono"><span class="meta-preview">'+esc(preview)+'</span></td></tr>';
  });
  wrap.innerHTML=h+'</tbody></table>';
  wrap.querySelectorAll('tbody tr').forEach(tr=>tr.addEventListener('click',()=>{
    const ev=state.telemetryRows[Number(tr.dataset.eventIndex)];
    openTelemetryDetail(ev);
  }));
  if($('#followNewest').checked && $('#telemetryOrder').value==='newest'){
    wrap.scrollTop=0;
  } else {
    wrap.scrollTop=prevTop;
  }
  wrap.scrollLeft=prevLeft;
}

async function showRaw(kind=state.rawKind) {
  state.rawKind=kind;
  state.rawMcp=$('#rawMcp').value;
  const r=await fetch('/api/raw/'+kind+'/'+encodeURIComponent(state.rawMcp),{cache:'no-store'});
  const t=await r.text();
  $('#rawSourceLabel').textContent=kind==='status'?'Status JSON':'Prometheus metrics';
  $('#rawOutput').textContent=kind==='status'?JSON.stringify(JSON.parse(t),null,2):t;
}

async function openRequestDetail(mcp,requestId) {
  const d=await api('/api/request/'+encodeURIComponent(mcp)+'/'+encodeURIComponent(requestId));
  const name=state.config?.tunnels?.[d.mcp]?.name||d.mcp;
  $('#requestModalTitle').textContent=name+' · '+(d.tool_name||d.rpc_method||'request');
  const box=(k,v)=>'<div class="detail-box"><div class="k">'+esc(k)+'</div><div class="v">'+v+'</div></div>';
  const input=d.input_json?esc(pretty(d.input_json)):'Payload not captured for this request.';
  const output=d.output_json?esc(pretty(d.output_json)):'Payload not captured for this request.';
  $('#requestModalBody').innerHTML=
    '<div class="detail-grid">'+
    box('Request ID','<span class="mono">'+esc(d.request_id)+'</span>')+
    box('Tool','<span class="mono">'+esc(d.tool_name||d.rpc_method||'—')+'</span>')+
    box('End-to-end',fmtMs(d.duration_ms))+
    box('Status',d.has_error?'<span class="bad">ERROR</span>':'<span class="ok">'+esc(d.status_code||'OK')+'</span>')+
    box('Input / enqueued',fmtTime(d.input_at))+
    box('MCP replied',fmtTime(d.mcp_reply_at))+
    box('OpenAI delivered',fmtTime(d.delivered_at))+
    box('Capture source',esc(d.capture_source||'not captured'))+
    '</div>'+
    '<div class="payload-grid"><div class="payload-panel"><h4>Input</h4><pre>'+input+'</pre></div>'+
    '<div class="payload-panel"><h4>Output</h4><pre>'+output+'</pre></div></div>'+
    (!d.input_json?'<div class="privacy-note">Historical requests before payload capture was enabled cannot be reconstructed. New tool calls are captured without OpenAI authentication headers or control-plane metadata.</div>':'');
  $('#requestModal').classList.add('open');
}

function openTelemetryDetail(ev) {
  let attrs={};
  try{attrs=JSON.parse(ev.attrs_json||'{}')}catch{attrs=ev.attrs_json||'{}'}
  $('#telemetryModalBody').innerHTML=
    '<div class="detail-grid">'+
    '<div class="detail-box"><div class="k">Time</div><div class="v">'+fmtTime(ev.ts)+'</div></div>'+
    '<div class="detail-box"><div class="k">MCP</div><div class="v">'+esc(state.config?.tunnels?.[ev.mcp]?.name||ev.mcp)+'</div></div>'+
    '<div class="detail-box"><div class="k">Level</div><div class="v">'+esc(ev.level)+'</div></div>'+
    '<div class="detail-box"><div class="k">Request</div><div class="v mono">'+esc(ev.request_id||'—')+'</div></div>'+
    '</div><div class="payload-panel" style="margin-top:14px"><h4>'+esc(ev.message)+'</h4><pre>'+esc(pretty(attrs))+'</pre></div>';
  $('#telemetryModal').classList.add('open');
}

async function loadAll() {
  if(state.refreshing)return;
  state.refreshing=true;
  $('#statusLine').textContent='Refreshing…';
  try{
    await Promise.all([loadOverview(),loadRequests(),loadHealth(),loadTelemetry()]);
    if($('#rawLive').checked)await showRaw(state.rawKind);
    $('#statusLine').textContent='Last refreshed '+new Date().toLocaleString()+
      ' · safe lifecycle telemetry · request payload capture strips control-plane headers';
  }catch(e){
    $('#statusLine').innerHTML='<span class="bad">Refresh failed: '+esc(e.message)+'</span>';
  }finally{
    state.refreshing=false;
  }
}

async function init() {
  state.config=await api('/api/config');
  Object.entries(state.config.tunnels).forEach(([slug,c])=>{
    [$('#mcpFilter'),$('#rawMcp')].forEach(sel=>{
      const o=document.createElement('option');o.value=slug;o.textContent=c.name;sel.appendChild(o);
    });
  });
  $('#rawMcp').value=state.rawMcp;
  applyPreset(360);
  await loadAll();
}

$$('.nav button').forEach(b=>b.addEventListener('click',()=>switchTab(b.dataset.tab)));
$$('.preset').forEach(b=>b.addEventListener('click',()=>{applyPreset(Number(b.dataset.min));loadAll()}));
$('#applyBtn').addEventListener('click',()=>{applyCustomRange();loadAll()});
$('#refreshBtn').addEventListener('click',loadAll);
$('#mcpFilter').addEventListener('change',e=>{state.mcp=e.target.value;loadAll()});
$('#overviewSort').addEventListener('change',loadOverview);
$('#requestSort').addEventListener('change',loadRequests);
$('#errorsOnly').addEventListener('change',loadRequests);
$('#levelFilter').addEventListener('change',loadTelemetry);
$('#telemetryOrder').addEventListener('change',loadTelemetry);
$('#telemetrySearchBtn').addEventListener('click',loadTelemetry);
$('#telemetrySearch').addEventListener('keydown',e=>{if(e.key==='Enter')loadTelemetry()});
$('#telemetryScale').addEventListener('input',e=>{
  const n=Number(e.target.value);
  document.documentElement.style.setProperty('--telemetry-scale',String(n/100));
  $('#telemetryScaleLabel').textContent=n+'%';
});
$('#rawMcp').addEventListener('change',()=>showRaw(state.rawKind));
$('#rawStatusBtn').addEventListener('click',()=>showRaw('status'));
$('#rawMetricsBtn').addEventListener('click',()=>showRaw('metrics'));
$$('[data-close-modal]').forEach(b=>b.addEventListener('click',()=>$('#'+b.dataset.closeModal).classList.remove('open')));
$$('.modal').forEach(m=>m.addEventListener('click',e=>{if(e.target===m)m.classList.remove('open')}));
document.addEventListener('keydown',e=>{if(e.key==='Escape')$$('.modal.open').forEach(m=>m.classList.remove('open'))});

$('#mcpLatency').addEventListener('click',e=>{
  const rect=e.currentTarget.getBoundingClientRect();
  const y=e.clientY-rect.top, x=e.clientX-rect.left;
  const hit=state.latencyHitboxes.find(h=>x>=h.x&&x<=h.x+h.w&&y>=h.y&&y<=h.y+h.h);
  if(!hit)return;
  state.mcp=hit.mcp;
  $('#mcpFilter').value=hit.mcp;
  $('#requestSort').value='newest';
  switchTab('requests');
  loadAll();
});

setInterval(()=>{
  if(!$('#autoRefresh').checked)return;
  if(state.activePreset)applyPreset(state.activePreset,true);
  loadAll();
},5000);

window.addEventListener('resize',()=>loadOverview().catch(()=>{}));
init().catch(e=>$('#statusLine').innerHTML='<span class="bad">Initialization failed: '+esc(e.message)+'</span>');
