const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const project = path.resolve(__dirname, '..');
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'webdav-folders-'));
const root = path.join(fixture, 'files');
fs.mkdirSync(path.join(root, 'MiShare'), {recursive:true});
const origin = 'http://127.0.0.1:18123';
const server = spawn('python3', ['server.py','--dev','--port','18123'], {
  cwd:project, env:{...process.env, DATA_DIR:path.join(fixture,'state'), LOCAL_ROOT:root,
    RCLONE_BINARY:process.env.RCLONE_BINARY || '/tmp/webdav-rclone-test/rclone-v1.75.0-osx-arm64/rclone'},
  stdio:['ignore','pipe','pipe']
});
let logs='';server.stderr.on('data', d=>logs+=d);
(async()=>{
  let browser;
  try {
    let ready=false;
    for(let i=0;i<60;i++){
      if(server.exitCode!==null)throw new Error(logs);
      try{ready=(await fetch(origin+'/healthz')).ok;}catch{}
      if(ready)break;await new Promise(r=>setTimeout(r,100));
    }
    assert(ready,'fixture server not ready');
    browser=await chromium.launch({headless:true});
    const page=await browser.newPage({viewport:{width:1280,height:900},hasTouch:true});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*.js*',async route=>{
      const response=await route.fetch();
      await route.fulfill({response,body:`(function(){\n${await response.text()}\n}).call(window);`});
    });
    await page.goto(origin);
    await page.getByText('共享未开启 · 暂无运行任务').waitFor();
    await page.locator('[data-browse="share"]').click();
    await page.locator('#createFolder:not([disabled])').waitFor();
    const create=async name=>{
      await page.locator('#createFolder').click();
      await page.locator('#folderEditForm input').fill(name);
      await page.locator('#saveFolder').click();
    };
    await create('测试文件夹');
    await page.locator('#folderEditDialog').waitFor({state:'hidden'});
    assert(fs.statSync(path.join(root,'MiShare/测试文件夹')).isDirectory());
    await create('测试文件夹');
    await page.locator('#folderEditError').filter({hasText:'同名'}).waitFor();
    await page.locator('#folderEditDialog [data-close]').first().click();
    const name=text=>page.locator('.folder-name').filter({hasText:new RegExp('^'+text+'$')});
    const editor=page.locator('.folder-inline-form input');
    await name('测试文件夹').click();
    await page.locator('#currentPath').filter({hasText:'/MiShare/测试文件夹'}).waitFor();
    await page.locator('#up').click();
    await name('测试文件夹').dblclick();
    await editor.fill('取消更名');await editor.press('Escape');
    assert(fs.existsSync(path.join(root,'MiShare/测试文件夹')));
    assert.equal(await page.locator('#browseDialog').isVisible(),true);
    await create('已存在');await page.locator('#folderEditDialog').waitFor({state:'hidden'});
    await name('测试文件夹').dblclick();
    await editor.fill('已存在');await editor.press('Enter');
    await page.locator('#browseError').filter({hasText:'同名'}).waitFor();
    await editor.fill('归档文件夹');await editor.press('Enter');
    await editor.waitFor({state:'hidden'});
    assert(fs.statSync(path.join(root,'MiShare/归档文件夹')).isDirectory());
    assert(!fs.existsSync(path.join(root,'MiShare/测试文件夹')));
    for(const width of [1280,393,320]){
      await page.setViewportSize({width,height:850});
      const before=await name('归档文件夹').locator('..').locator('..').boundingBox();
      if(width===1280)await name('归档文件夹').dblclick();
      else {await name('归档文件夹').tap();await name('归档文件夹').tap();}
      await editor.fill('新的中文文件夹名称');
      const after=await page.locator('.folder-inline-form').locator('..').boundingBox();
      assert(Math.abs(before.y-after.y)<2,'inline editing should not move the row');
      for(const selector of ['#browseDialog','.folder-inline-form']){
        assert(await page.locator(selector).evaluate(el=>el.scrollWidth<=el.clientWidth+1));
      }
      for(const img of await page.locator('dialog[open] img').all())await img.evaluate(i=>i.decode());
      await page.screenshot({path:path.join(project,`qa-inline-folders-${width}.png`)});
      await editor.press('Escape');
    }
    await name('归档文件夹').dblclick();await editor.fill('不要保存');
    await page.locator('#browseTitle').click();
    await editor.waitFor({state:'hidden'});
    assert(!fs.existsSync(path.join(root,'MiShare/不要保存')));
    await page.locator('#up').click();
    await name('MiShare').dblclick();
    await page.locator('#browseError').filter({hasText:'一级目录'}).waitFor();
    assert.deepEqual(errors,[]);
    assert.equal(await page.locator('.folder-rename').count(),0);
    console.log('PASS: single-click open, double-click inline rename, Enter save, Escape cancel, touch double-tap, conflicts, protected directories, sandbox scopes and 1280/393/320 layouts');
  }finally{
    if(browser)await browser.close();
    if(server.exitCode===null){server.kill('SIGTERM');await once(server,'exit');}
    fs.rmSync(fixture,{recursive:true,force:true});
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
