const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const origin = process.env.PREVIEW_URL || 'http://127.0.0.1:18122';
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const split of [true, false]) {
      const page = await browser.newPage({viewport:{width:1280,height:900}});
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      // Reproduce the host's separate Function execution contexts, not just rewritten URLs.
      await page.route('**/*.js*', async route => {
        const response = await route.fetch();
        await route.fulfill({response,body:`(function(){\n${await response.text()}\n}).call(window);`});
      });
      if (split) await page.route(origin+'/', async route => {
        const response = await route.fetch();
        await route.fulfill({response, body:(await response.text()).replace(
          /<script src="app\.bundle\.js[^\"]*" defer><\/script>/,
          '<script src="app.js" defer></script><script src="access-ui.js" defer></script>')});
      });
      // Toggle responses are isolated fixtures: this test never opens a NAS network listener.
      const actions = [];
      let enabled = false;
      await page.route('**/api/access/*', async route => {
        const action = route.request().url().split('/').pop();
        actions.push(action); enabled = action === 'start';
        await route.fulfill({json:{ok:true}});
      });
      await page.route('**/api/status', async route => {
        const response = await route.fetch();
        const data = await response.json();
        data.access.enabled = enabled; data.access.running = enabled;
        await route.fulfill({json:data});
      });
      await page.goto(origin+'/');
      await page.getByRole('button',{name:'用户与权限',exact:true}).click();
      await page.getByText('共享未开启 · 暂无运行任务').waitFor();
      if (split) {
        assert(errors.some(e => e.includes('$ is not defined')), errors.join('\n'));
        assert.equal(await page.locator('#toggleAccess').evaluate(e=>e.onclick), null);
        console.log('REPRODUCED: split script loses shared lexical scope; all access controls unbound');
      } else {
        await page.locator('#accessFolders article').first().waitFor();
        await page.locator('#accessUsers article').first().waitFor();
        await page.locator('#addAccessFolder').click();
        await page.locator('#accessFolderDialog').waitFor({state:'visible'});
        await page.locator('#accessFolderDialog [data-close]').click();
        await page.locator('#addAccessUser').click();
        await page.locator('#accessUserDialog').waitFor({state:'visible'});
        await page.locator('#accessUserDialog [data-close]').click();
        await page.locator('#toggleAccess').click();
        await page.getByRole('button',{name:'关闭多用户共享',exact:true}).waitFor();
        assert(await page.locator('#addAccessUser').isDisabled());
        await page.locator('#toggleAccess').click();
        await page.getByRole('button',{name:'开启多用户共享',exact:true}).waitFor();
        assert.deepEqual(actions,['start','stop']);
        for (const width of [1280,393,320]) {
          await page.setViewportSize({width,height:900});
          assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
          for (const img of await page.locator('#access img').all()) await img.evaluate(i=>i.decode());
          await page.screenshot({path:path.resolve(__dirname,`../qa-sandbox-${width}.png`),fullPage:true});
        }
        assert.deepEqual(errors,[]);
        console.log('PASS: combined entry; accounts/directories; add dialogs; start/stop requests; desktop/mobile');
      }
      await page.close();
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
