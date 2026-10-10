/** Full browser regression for legacy MCP Observatory features + Grafana. */
const assert = require('node:assert/strict');
const {chromium} = require('/home/virat/Projects/playwright-mcp/node_modules/playwright');
const base=process.env.BASE_URL || 'https://desktop-ubuntu.tailac2e85.ts.net:8470';
let currentPage=null;
const checks=[];
const fails=[];
const errors=[];
const requestErrors=[];
const log=async(name,fn)=>{
  try { await fn();checks.push(name);console.log('PASS',name);}
  catch(e){fails.push({name,error:e.message});console.error('FAIL',name,e.message.slice(0,320));}
  finally {try {await currentPage?.evaluate(()=>document.querySelectorAll('.modal.open').forEach(e=>e.classList.remove('open')))}catch(e){}}
};
async function waitForIdle(page){await page.waitForFunction(()=>!document.querySelector('#statusLine')?.textContent?.includes('Refreshing'),{timeout:12000}).catch(()=>{});}
async function pull(path){const response=await fetch(base+path);assert.equal(response.status,200,`${path} response status`);return response.json()}
async function run(){
  const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,args:['--no-sandbox','--disable-gpu']});
  const page=await browser.newPage({viewport:{width:1730,height:1150},ignoreHTTPSErrors:false});
  page.setDefaultTimeout(8000);currentPage=page;
  page.on('pageerror',e=>errors.push(e.message));
  page.on('response',res=>{if(res.status()>=500)requestErrors.push(`${res.status()}: ${res.url().slice(0,100)}`)});
  const q=(s)=>page.locator(s);
  async function tab(s){await q(`[data-tab="${s}"]`).click();await q(`#tab-${s}.active`).waitFor();}
  async function pauseAuto(){await q('#autoRefresh').uncheck();}
  try{
    await log('HTTPS, initial page and seven overview KPIs',async()=>{const r=await page.goto(base+'/',{waitUntil:'domcontentloaded'});assert.equal(r.status(),200);await q('#kpis .kpi').first().waitFor({timeout:15000});await page.waitForFunction(()=>document.querySelectorAll('#kpis .kpi').length===7);assert.equal(await q('#kpis .kpi').count(),7);await pauseAuto();});
    await log('Five navigation tabs, including four unchanged legacy tabs',async()=>{assert.deepEqual(await q('.nav button').allTextContents(),['Overview','Requests','Health','Telemetry','Grafana']);});
    await log('MCP filter includes six named MCPs and all',async()=>{const opts=await q('#mcpFilter option').allTextContents();assert.equal(opts.length,7);assert.equal(opts[0],'All MCPs');});
    await log('Default overview newest table, timeline and latency canvases',async()=>{assert.ok(await q('#overviewTable tr.request-row').count()>0);for(const canvas of ['#timeline','#mcpLatency'])assert.ok(await q(canvas).evaluate(e=>e.width>0&&e.height>0));assert.match(await q('#bucketLabel').innerText(),/bucket/);});
    for(const min of [15,60,360,1440,10080]) await log(`Preset range ${min} minutes`,async()=>{
      await q(`.preset[data-min="${min}"]`).click();await waitForIdle(page);
      assert.equal(await q(`.preset.active`).getAttribute('data-min'),String(min));
      const diff=await page.evaluate(()=>((new Date(document.querySelector('#toInput').value))-(new Date(document.querySelector('#fromInput').value)))/60000);
      assert.ok(Math.abs(diff-min)<=1,`selected range ${diff}`);
      assert.ok(await q('#kpis .kpi').count()===7);
    });
    await log('Custom datetime range applies without active preset',async()=>{
      await q('#fromInput').fill('2026-10-07T00:00');await q('#toInput').fill('2026-10-09T00:00');await q('#applyBtn').click();await waitForIdle(page);
      assert.equal(await q('.preset.active').count(),0);assert.match(await q('#rangeStatus').innerText(),/custom range/);
    });
    await log('Manual Refresh retains correct status after custom range',async()=>{await q('#refreshBtn').click();await page.waitForFunction(()=>document.querySelector('#statusLine').textContent.includes('Last refreshed'),null,{timeout:12000});});
    await log('MCP filter applies to overview rows and request table',async()=>{
      await q('#mcpFilter').selectOption('terminal');await waitForIdle(page);
      await tab('requests');await q('#requestTable tr.request-row').first().waitFor({timeout:10000});
      const rows=await q('#requestTable tr.request-row').evaluateAll(els=>els.slice(0,40).map(x=>x.dataset.mcp));
      assert.ok(rows.length>0);assert.ok(rows.every(x=>x==='terminal'));
    });
    await log('Overview request sort choices and result ordering',async()=>{
      await tab('overview');await q('#mcpFilter').selectOption('all');
      for(const sort of ['newest','slowest','fastest','oldest']){
        await q('#overviewSort').selectOption(sort); await page.evaluate(()=>loadOverview()); await q('#overviewTable tr.request-row').first().waitFor({timeout:8000});
        const ids=await q('#overviewTable tr.request-row').evaluateAll(a=>a.slice(0,5).map(x=>x.dataset.request));
        const data=await page.evaluate(async sort=>{const r=await fetch('/api/requests?'+qparams({sort,limit:40}));return r.json()},sort);
        assert.deepEqual(ids,data.rows.slice(0,ids.length).map(x=>x.request_id),`overview sort ${sort}`);
      }
    });
    await log('Clickable MCP latency bars filter into Requests tab',async()=>{
      await tab('overview');await q('#mcpLatency').scrollIntoViewIfNeeded();
      const hit=await page.evaluate(()=>state.latencyHitboxes.find(x=>x.mcp==='terminal'));
      assert.ok(hit,'terminal canvas hitbox');
      const b=await q('#mcpLatency').boundingBox();
      await page.mouse.click(b.x+12,b.y+hit.y+hit.h/2);await q('#tab-requests.active').waitFor({timeout:7000});
      assert.equal(await q('#mcpFilter').inputValue(),'terminal');
    });
    await log('Request list defaults/newest/oldest/slowest/fastest/reply-delay sorting',async()=>{
      await q('#mcpFilter').selectOption('all');await waitForIdle(page);
      for(const sort of ['newest','oldest','slowest','fastest','reply_delay']){
        await q('#requestSort').selectOption(sort);
        await page.evaluate(()=>loadRequests());
        await q('#requestTable tr.request-row').first().waitFor({timeout:8000});
        const ids=await q('#requestTable tr.request-row').evaluateAll(a=>a.slice(0,5).map(x=>x.dataset.request));
        const data=await page.evaluate(async sort=>{const r=await fetch('/api/requests?'+qparams({sort,limit:500,errors_only:false}));return r.json()},sort);
        assert.deepEqual(ids,data.rows.slice(0,ids.length).map(x=>x.request_id),`request sort ${sort}`);
      }
    });
    await log('Errors-only filter restricts rows to errors',async()=>{
      const checked=page.waitForResponse(r=>r.url().includes('/api/requests?')&&r.url().includes('errors_only=true'));
      await q('#errorsOnly').check();await checked;await page.waitForFunction(()=>document.querySelector('#requestTable .empty')||document.querySelector('#requestTable tr.request-row')?.textContent.includes('ERROR'),null,{timeout:8000});const list=await q('#requestTable tr.request-row').count();
      const data=await page.evaluate(async()=>{const r=await fetch('/api/requests?'+qparams({sort:document.querySelector('#requestSort').value,errors_only:true,limit:500}));return r.json()});
      assert.equal(list,data.rows.length);assert.ok(data.rows.every(x=>x.has_error||x.status_code>=400));
      const unchecked=page.waitForResponse(r=>r.url().includes('/api/requests?')&&r.url().includes('errors_only=false'));
      await q('#errorsOnly').uncheck();await unchecked;await q('#requestTable tr.request-row').first().waitFor({timeout:10000});
    });
    await log('Request modal input/output and all lifecycle metadata',async()=>{
      await q('#requestTable tr.request-row').first().waitFor();await q('#requestTable tr.request-row').first().click();await q('#requestModal.open').waitFor();
      const labels=await q('#requestModalBody .detail-box .k').allTextContents();
      for(const k of ['Request ID','Tool','End-to-end','Status','MCP replied','OpenAI delivered','Capture source'])assert.ok(labels.includes(k),k);
      const heads=await q('#requestModalBody h4').allTextContents();assert.deepEqual(heads,['Input','Output']);
      assert.equal(await q('#requestModalBody .payload-panel pre').count(),2);
    });
    await log('Request modal Escape key, overlay, and Close button',async()=>{
      await page.keyboard.press('Escape');await q('#requestModal').waitFor({state:'hidden'});
      await q('#requestTable tr.request-row').first().click();await q('#requestModal.open').waitFor();await q('#requestModal [data-close-modal]').click();await q('#requestModal').waitFor({state:'hidden'});
      await q('#requestTable tr.request-row').first().click();await q('#requestModal.open').waitFor();
      await q('#requestModal').click({position:{x:5,y:5}});await q('#requestModal').waitFor({state:'hidden'});
    });
    await log('Health view six MCP cards and status fields',async()=>{
      await tab('health');const h=await q('#healthGrid .card').count();assert.equal(h,6);
      const txt=await q('#healthGrid').innerText();for(const k of ['Health','Ready','Uptime','Tool calls','Poll errors','Queue','Workers','RSS','Net RX / TX'])assert.ok(txt.includes(k),k);
    });
    await log('Telemetry table and newest-first default',async()=>{
      await tab('telemetry');await q('#telemetryTable tbody tr').first().waitFor({timeout:12000});assert.equal(await q('#telemetryOrder').inputValue(),'newest');
      const dt=await q('#telemetryTable tbody tr').first().locator('td').first().innerText();assert.ok(dt.length>5);
    });
    await log('Telemetry oldest-first ordering',async()=>{
      await q('#telemetryOrder').selectOption('oldest');await q('#telemetryTable tbody tr').first().waitFor();
      const raw=await page.evaluate(async()=>{const u=new URLSearchParams({order:'oldest',limit:'800',mcp:document.querySelector('#mcpFilter').value,level:document.querySelector('#levelFilter').value,q:document.querySelector('#telemetrySearch').value});return (await fetch('/api/telemetry?'+u)).json()});
      const table=await q('#telemetryTable tbody tr').count();assert.equal(table,raw.rows.length);
    });
    await log('Telemetry DEBUG, INFO, WARN filters',async()=>{
      for(const level of ['debug','info','warn']){
        await q('#levelFilter').selectOption(level);await page.waitForTimeout(350);
        const levels=await q('#telemetryTable tbody tr td:nth-child(3)').allTextContents();
        assert.ok(levels.every(x=>x.toLowerCase()===level),`${level}: ${levels.slice(0,3)}`);
      }
      await q('#levelFilter').selectOption('all');
    });
    await log('Telemetry free-text search by event metadata',async()=>{
      await q('#telemetryOrder').selectOption('newest');
      await q('#telemetrySearch').fill('dispatcher');await q('#telemetrySearchBtn').click();
      await q('#telemetryTable tbody tr').first().waitFor({timeout:10000});
      const n=await q('#telemetryTable tbody tr').count();assert.ok(n>0);
      await q('#telemetrySearch').fill('');await q('#telemetrySearch').press('Enter');
      await q('#telemetryTable tbody tr').first().waitFor();
    });
    await log('Telemetry click-to-open full metadata modal',async()=>{
      await q('#telemetryTable tbody tr').first().click();await q('#telemetryModal.open').waitFor({timeout:7000});
      const labels=await q('#telemetryModalBody .detail-box .k').allTextContents();for(const k of ['Time','MCP','Level','Request'])assert.ok(labels.includes(k),k);
      await q('#telemetryModal [data-close-modal]').click();await q('#telemetryModal').waitFor({state:'hidden'});
    });
    await log('Telemetry horizontal scrolling and 80-140% scaling',async()=>{
      const wrap=q('#telemetryTable');const dims=await wrap.evaluate(e=>({scroll:e.scrollWidth,client:e.clientWidth}));assert.ok(dims.scroll>dims.client,JSON.stringify(dims));
      await wrap.evaluate(e=>e.scrollLeft=140);const x=await wrap.evaluate(e=>e.scrollLeft);assert.ok(x>0);
      for(const v of ['80','100','140']){
        await q('#telemetryScale').evaluate((e,v)=>{e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}))},v);
        assert.equal(await q('#telemetryScaleLabel').innerText(),v+'%');
      }
    });
    await log('Follow-newest checkbox controls scroll reset',async()=>{
      await q('#followNewest').check();await q('#telemetryOrder').selectOption('newest');
      await q('#telemetryTable').evaluate(e=>e.scrollTop=200);await q('#telemetrySearchBtn').click();await page.waitForTimeout(400);
      const pos=await q('#telemetryTable').evaluate(e=>e.scrollTop);assert.equal(pos,0);
      await q('#followNewest').uncheck();await q('#telemetryTable').evaluate(e=>e.scrollTop=120);await q('#telemetrySearchBtn').click();await page.waitForTimeout(400);
      const pos2=await q('#telemetryTable').evaluate(e=>e.scrollTop);assert.ok(pos2>=80,`preserved scroll ${pos2}`);
    });
    await log('Raw Status JSON per MCP',async()=>{
      await q('#rawMcp').selectOption('terminal');await q('#rawStatusBtn').click();await page.waitForFunction(()=>document.querySelector('#rawSourceLabel')?.textContent==='Status JSON');
      const value=await q('#rawOutput').innerText();const st=JSON.parse(value);assert.ok(st&&typeof st==='object');
    });
    await log('Raw Prometheus metrics per MCP',async()=>{
      await q('#rawMcp').selectOption('playwright');await q('#rawMetricsBtn').click();await page.waitForFunction(()=>document.querySelector('#rawSourceLabel')?.textContent==='Prometheus metrics');
      assert.match(await q('#rawOutput').innerText(),/command_end_to_end_latency|process_cpu_seconds_total/);
    });
    await log('Raw auto-update toggle and MCP source switching',async()=>{
      await q('#rawLive').uncheck();assert.ok(!await q('#rawLive').isChecked());
      await q('#rawMcp').selectOption('terminal');await q('#rawMetricsBtn').click();assert.match(await q('#rawOutput').innerText(),/command_end_to_end_latency/);
      await q('#rawLive').check();assert.ok(await q('#rawLive').isChecked());
    });
    await log('Auto-refresh on/off and manual refresh while off',async()=>{
      assert.ok(!await q('#autoRefresh').isChecked());await q('#refreshBtn').click();await page.waitForFunction(()=>document.querySelector('#statusLine').textContent.includes('Last refreshed'));
      await q('#autoRefresh').check();assert.ok(await q('#autoRefresh').isChecked());await q('#autoRefresh').uncheck();
    });
    await log('Live auto-refresh performs an actual periodic API fetch',async()=>{
      await q('#autoRefresh').check();
      const response=await page.waitForResponse(r=>r.url().includes('/api/summary?')&&r.status()===200,{timeout:12000});
      assert.equal(response.status(),200);
      await q('#autoRefresh').uncheck();
    });
    await log('Grafana embedded MCP panels and provisioned dashboard',async()=>{
      await tab('grafana');const frame=page.frameLocator('#grafanaFrame');
      await frame.getByText('MCP tunnel health',{exact:true}).waitFor({timeout:30000});
      await frame.getByText('P95 end-to-end latency',{exact:true}).waitFor({timeout:30000});
      const r=await page.request.get(base+'/grafana/api/dashboards/uid/mcp-observatory-v2');assert.equal(r.status(),200);
      const pan=(await r.json()).dashboard.panels;
      const titles=pan.map(x=>x.title);assert.equal(titles.length,12);for(const k of ['Request volume (5m)','P50 end-to-end latency','P95 end-to-end latency','P99 end-to-end latency','Queued commands','Worker occupancy'])assert.ok(titles.includes(k));
    });
    await log('Grafana backend Prometheus all six tunnels up',async()=>{
      const u=base+'/grafana/api/datasources/proxy/uid/mcp-prometheus/api/v1/query?query='+encodeURIComponent('up{job="mcp-tunnels"}');
      const r=await page.request.get(u);assert.equal(r.status(),200);const arr=(await r.json()).data.result;assert.equal(arr.length,6);assert.ok(arr.every(x=>x.value[1]==='1'));
    });
  }finally{
    await browser.close();
    console.log(JSON.stringify({summary:{passed:checks.length,failed:fails.length,pageErrors:errors.length,serverErrors:requestErrors.length},fails,errors,requestErrors},null,2));
    if(fails.length||errors.length||requestErrors.length)process.exitCode=1;
  }
}
run().catch(e=>{console.error('FATAL',e.stack);process.exitCode=1});
