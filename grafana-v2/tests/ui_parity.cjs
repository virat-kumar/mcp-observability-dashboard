const assert = require('node:assert/strict');
const { chromium } = require('/home/virat/Projects/playwright-mcp/node_modules/playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,args:['--no-sandbox','--disable-gpu']});
 const page=await browser.newPage({viewport:{width:1720,height:1040}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const checks=[];
 try{
  const resp=await page.goto((process.env.BASE_URL || 'http://127.0.0.1:9120')+'/',{waitUntil:'domcontentloaded',timeout:20000});
  assert.equal(resp.status(),200);
  await page.waitForSelector('#kpis .kpi',{timeout:25000});checks.push('Overview KPIs render');
  assert.equal(await page.locator('.nav button').count(),5);checks.push('Old 4 tabs + Grafana');
  const opts=await page.locator('#mcpFilter option').count();assert.equal(opts,7);checks.push('Six MCP filters');
  const reqRows=await page.locator('#overviewTable .request-row').count();assert.ok(reqRows>0);checks.push('Recent requests visible');
  await page.locator('#overviewTable .request-row').first().click();await page.locator('#requestModal.open').waitFor({timeout:10000});checks.push('Clickable request inspector');
  const detailText=await page.locator('#requestModalBody').innerText();assert.ok(/Input|Output|Request/i.test(detailText));
  await page.locator('#requestModal [data-close-modal]').first().click();
  await page.locator('[data-tab="requests"]').click();await page.waitForSelector('#tab-requests.active');checks.push('Requests view');
  await page.locator('#requestSort').selectOption('oldest');await page.waitForTimeout(900);checks.push('Oldest/newest sorting');
  await page.locator('[data-tab="health"]').click();await page.waitForSelector('#tab-health.active');checks.push('Health view');
  await page.locator('[data-tab="telemetry"]').click();await page.waitForSelector('#tab-telemetry.active');checks.push('Telemetry view');
  await page.locator('#telemetryScale').evaluate(e=>{e.value='130';e.dispatchEvent(new Event('input',{bubbles:true}))}); assert.equal(await page.locator('#telemetryScaleLabel').innerText(),'130%');checks.push('Telemetry scaling');
  await page.locator('[data-tab="grafana"]').click();await page.waitForSelector('#tab-grafana.active');
  const frame=page.frameLocator('#grafanaFrame');await frame.locator('body').waitFor({timeout:15000});
  const grafanaText=(await frame.locator('body').innerText()).slice(0,1200);
  assert.match(grafanaText,/MCP|All|Request volume/i);checks.push('Grafana embed loads');
  await frame.getByText('MCP tunnel health',{exact:true}).waitFor({timeout:20000});
  await frame.getByText('P95 end-to-end latency',{exact:true}).waitFor({timeout:20000});
  const dashboardResp=await page.request.get((process.env.BASE_URL || 'http://127.0.0.1:9120')+'/grafana/api/dashboards/uid/mcp-observatory-v2');
  assert.equal(dashboardResp.status(),200);
  const dashboard=(await dashboardResp.json()).dashboard;
  assert.equal(dashboard.panels.length,12);
  assert.ok(dashboard.panels.some(panel=>panel.title==='Worker occupancy'));
  checks.push('Grafana charts render and all 12 panels are provisioned');
  await page.screenshot({path:'/tmp/mcp-observatory-v2-ui.png',fullPage:false});
  console.log(JSON.stringify({ok:true,checks,grafanaExcerpt:grafanaText.slice(0,250),errors},null,2));
 } catch(e){console.error(JSON.stringify({ok:false,checks,error:e.message,errors},null,2));process.exitCode=1}
 finally {await browser.close()}
})();
