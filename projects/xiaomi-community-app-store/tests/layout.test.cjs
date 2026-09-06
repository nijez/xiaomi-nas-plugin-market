const { test } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');

const web = path.resolve(__dirname, '../web');
const names = ['qB 下载', 'WebDAV 文件桥', '设备管家', '115 云备份', '阿里云盘备份'];

test('standalone and Mac micro-app layouts keep the last plugin reachable', async () => {
  const browser = await chromium.launch({ headless: true });
  const html = await fs.readFile(path.join(web, 'index.html'), 'utf8');
  const css = await fs.readFile(path.join(web, 'styles.css'), 'utf8');
  const script = await fs.readFile(path.join(web, 'app.js'), 'utf8');
  const output = process.env.LAYOUT_SCREENSHOTS;
  if (output) await fs.mkdir(output, { recursive: true });
  try {
    for (const mode of ['standalone', 'micro-app']) {
      for (const width of [1280, 760, 393, 320]) {
        const page = await browser.newPage({ viewport: { width: mode === 'micro-app' ? 1440 : width, height: 900 } });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/api/catalog', route => route.fulfill({ json: {
          ok: true, preview: true, catalog: { packages: names.map((name, i) => ({
            id: String(i), name, summary: '测试用插件说明，不执行安装或卸载', icon: 'fixture.png',
            version: '0.1.0-rc1', installedVersion: '0.1.0-rc1', managed: true,
          })) },
        } }));
        await page.route('**/catalog/fixture.png', route => route.fulfill({
          path: path.join(web, 'assets/community-store-v4.png'), contentType: 'image/png',
        }));
        await page.route('http://layout.test/', route => route.fulfill({ contentType: 'text/html', body: '<html><body></body></html>' }));
        await page.goto('http://layout.test/');
        await page.evaluate(({ html, css, mode, width }) => {
          const parsed = new DOMParser().parseFromString(html, 'text/html');
          parsed.querySelectorAll('script').forEach(el => el.remove());
          document.head.append(...parsed.querySelectorAll('meta'));
          const sheet = document.createElement('style');
          // Reproduce the client's html/body remapping and inherited large root font.
          sheet.textContent = mode === 'micro-app' ? css.replace(/\bhtml\b/g, 'micro-app').replace(/\bbody\b/g, 'micro-app-body') : css;
          document.head.append(sheet);
          if (mode === 'micro-app') {
            const hostStyle = document.createElement('style');
            hostStyle.textContent = 'html{font-size:39.2px}html,body{height:100%;margin:0;overflow:hidden}.fixture-host{overflow:hidden;height:420px}micro-app,micro-app-body{display:block;height:100%}';
            document.head.append(hostStyle);
            const host = document.createElement('div');
            host.className = 'fixture-host';
            host.style.width = width + 'px';
            host.innerHTML = '<micro-app><micro-app-body></micro-app-body></micro-app>';
            host.querySelector('micro-app-body').append(...parsed.body.childNodes);
            document.body.append(host);
          } else {
            document.body.append(...parsed.body.childNodes);
          }
        }, { html, css, mode, width });
        await page.addScriptTag({ content: script });
        await page.locator('#packageList .package-item').last().waitFor();
        const root = page.locator('#community-store-app');
        const metrics = await root.evaluate(el => ({
          width: el.clientWidth, scrollWidth: el.scrollWidth, height: el.clientHeight,
          font: getComputedStyle(el).fontSize,
          button: getComputedStyle(el.querySelector('.tab')).fontSize,
          small: getComputedStyle(el.querySelector('.package-copy small')).fontSize,
          columns: getComputedStyle(el.querySelector('.package-grid')).gridTemplateColumns.split(' ').length,
        }));
        assert.equal(metrics.font, '15px');
        assert.equal(metrics.button, '15px');
        assert.equal(metrics.small, '12px');
        assert.ok(metrics.scrollWidth <= metrics.width + 1, JSON.stringify({ mode, width, metrics }));
        assert.equal(metrics.columns, width <= 760 ? 1 : 2);
        if (mode === 'micro-app') {
          assert.equal(metrics.height, 420);
          assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).fontSize), '39.2px');
        }
        for (const view of ['featured', 'installed']) {
          await page.locator(`[data-view="${view}"]`).click();
          await root.hover();
          await page.mouse.wheel(0, 5000);
          await page.waitForFunction(() => {
            const root = document.getElementById('community-store-app');
            return root.scrollTop + root.clientHeight >= root.scrollHeight - 2;
          });
          const last = page.locator(`#${view === 'featured' ? 'packageList' : 'installedList'} .package-item`).last();
          const bounds = await last.boundingBox();
          const frame = await root.boundingBox();
          assert.ok(bounds.y >= frame.y && bounds.y + bounds.height <= frame.y + frame.height + 1);
          if (output && view === 'featured') await root.screenshot({ path: path.join(output, `${mode}-${width}-bottom.png`) });
        }
        await root.focus();
        await page.keyboard.press('Control+Home');
        await page.locator('[data-view="sources"]').click();
        await page.locator('.source-band').waitFor({ state: 'visible' });
        assert.deepEqual(errors, []);
        assert.equal(await page.locator('img').evaluateAll(els => els.every(e => e.complete && e.naturalWidth > 0)), true);
        console.log(`${mode} ${width}px: fonts, containment, wheel scroll, last card, tabs, images passed`);
        await page.close();
      }
    }
  } finally {
    await browser.close();
  }
});
