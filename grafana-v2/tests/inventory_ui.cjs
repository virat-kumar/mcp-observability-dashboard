const assert = require('node:assert/strict');
const inventory = require('../app/mcp_inventory.json');
const {chromium} = require('/home/virat/Projects/playwright-mcp/node_modules/playwright');

async function main() {
  const browser = await chromium.launch({executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox', '--disable-gpu']});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    const response = await page.goto(process.env.BASE_URL || 'http://127.0.0.1:29120/', {waitUntil: 'domcontentloaded'});
    assert.equal(response.status(), 200);
    await page.waitForFunction(() => document.querySelectorAll('#healthGrid .card').length === 14);
    const options = await page.locator('#mcpFilter option').evaluateAll(elements => elements.map(element => element.value));
    assert.deepEqual(options.filter(value => value !== 'all').sort(), Object.keys(inventory).sort());
    assert.equal(await page.locator('#healthGrid .card').count(), Object.keys(inventory).length);
    const terminal = page.locator('#healthGrid .card').filter({has: page.locator('h2', {hasText: 'Ubuntu Terminal OAuth'})});
    assert.match(await terminal.innerText(), /Deprecated versions \(retired\)/);
    assert.match(await terminal.innerText(), /8901/);
    const neo4j = page.locator('#healthGrid .card').filter({has: page.locator('h2', {hasText: 'Neo4j MCP'})});
    assert.match(await neo4j.innerText(), /TOOLS UNVERIFIED/);
    const geometry = await page.evaluate(() => ({height: document.querySelector('#mcpLatency').clientHeight, boxes: state.latencyHitboxes}));
    assert.equal(geometry.boxes.length, Object.keys(inventory).length);
    assert.ok(geometry.boxes.every(box => box.y + box.h <= geometry.height));
    assert.deepEqual(errors, []);
    await page.screenshot({path: '/tmp/mcp-observatory-inventory.png', fullPage: true});
    console.log('PASS: inventory filters, combined retired versions, health-only labels, chart geometry and page JavaScript');
  } finally {
    await browser.close();
  }
}

main().catch(error => {console.error(error); process.exitCode = 1;});
