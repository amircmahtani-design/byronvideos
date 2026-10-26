// Optional browser QA: `node web/ui.test.cjs` with Playwright and Chromium installed.
// API responses below are TEST DOUBLE data, not claims of a real renderer.
const {chromium} = require('playwright');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = __dirname;
const types = {'.html':'text/html','.css':'text/css','.mjs':'text/javascript'};
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1cAAAAASUVORK5CYII=','base64');
(async () => {
  const server = http.createServer(async(req,res) => {
    try {
      const name = req.url === '/' ? 'index.html' : req.url.slice(1);
      if (!['index.html','styles.css','app.mjs','core.mjs'].includes(name)) {res.writeHead(404);res.end();return;}
      res.setHeader('Content-Type',types[path.extname(name)]);res.end(await fs.readFile(path.join(root,name)));
    } catch {res.writeHead(500);res.end();}
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try { browser = await chromium.launch({executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium',headless:true,args:['--no-sandbox']}); } catch(error) { await new Promise(resolve=>server.close(resolve)); throw error; }
  const page = await browser.newPage({viewport:{width:1024,height:1366}});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  try {
    await page.goto(url);
    await page.waitForFunction(()=>document.querySelector('#connection').textContent.includes('Renderer unavailable'));
    assert.equal(await page.locator('#convert').isDisabled(),true);
    await page.screenshot({path:path.join(root,'qa-ipad.png'),fullPage:true});
    console.log('PASS: unavailable backend clearly disables Convert; iPad screenshot saved.');
    await page.route('**/api/health', route => route.fulfill({json:{ready:true,mode:'local'}}));
    await page.locator('#reconnect').click();
    await page.waitForFunction(()=>!document.querySelector('#convert').disabled);
    const exactScript='A synthetic test — exact wording, preserved.\n\nA second paragraph.';
    await page.locator('#script').fill(exactScript);
    await page.locator('#images').setInputFiles(['one.png','two.png','three.png','four.png'].map(name=>({name,mimeType:'image/png',buffer:png})));
    assert.equal(await page.locator('.painting').count(),4);
    await page.getByRole('button',{name:'Move earlier: two.png',exact:true}).click();
    assert.match(await page.locator('.painting-name').first().textContent(),/^two.png/);
    let posted=0,body='';
    let status='running';
    await page.route('**/api/jobs', async route => {posted++;body=route.request().postData();await route.fulfill({json:{id:'testjob123'}});});
    await page.route('**/api/jobs/testjob123', route => route.fulfill({json:status==='running'?{status:'running',progress:23,message:'Test double rendering'}:{status:'complete',progress:100,message:'Test double finished',download_url:'/api/jobs/testjob123/video'}}));
    await page.locator('#convert').click();
    await page.waitForFunction(()=>document.querySelector('#progress').value===23);
    await page.locator('#creator').dispatchEvent('submit');
    assert.equal(posted,1);
    assert.ok(body.includes(exactScript.replace(/\n/g,'\r\n')) || body.includes(exactScript));
    assert.ok(body.indexOf('filename="two.png"') < body.indexOf('filename="one.png"'));
    console.log('PASS: exact script and ordered original files sent once; repeated submit blocked.');
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#progress').value===23);
    assert.equal(posted,1);
    console.log('PASS: reload reconnects to existing job without reuploading private inputs.');
    status='complete';
    await page.locator('#download').waitFor({state:'visible',timeout:6000});
    assert.equal(await page.locator('#download').getAttribute('href'),url+'/api/jobs/testjob123/video');
    await page.locator('#new-film').click();
    await page.waitForFunction(()=>!document.querySelector('#convert').disabled);
    console.log('PASS: verified completion response exposes local download; make-another unlocks.');
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(root,'qa-phone.png'),fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
    await page.locator('#script').fill('<script>alert(1)</script> Exact text.');
    await page.locator('#images').setInputFiles(['1.png','2.png','3.png','4.png'].map(name=>({name,mimeType:'image/png',buffer:png})));
    await page.unroute('**/api/jobs');
    await page.route('**/api/jobs',route=>route.abort('connectionreset'));
    await page.locator('#convert').click();
    await page.waitForFunction(()=>document.querySelector('#error').textContent.includes('may already have accepted'));
    assert.equal(await page.locator('#convert').isDisabled(),true);
    assert.equal(await page.locator('#script').inputValue(),'<script>alert(1)</script> Exact text.');
    assert.deepEqual(errors,[]);
    console.log('PASS: uncertain POST does not auto-resubmit; text not interpreted; no page exceptions or phone overflow.');
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
