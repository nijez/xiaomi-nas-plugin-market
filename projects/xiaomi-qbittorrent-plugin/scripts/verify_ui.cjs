// Browser-only fixtures; no NAS, Docker, peers, or real downloads are contacted.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless:true});
  const out = path.join(__dirname,'../test-results'); fs.mkdirSync(out,{recursive:true});
  try {
    for (const width of [1280,768,393,320]) {
      const page = await browser.newPage({viewport:{width,height:900}});
      const errors = []; page.on('pageerror',e=>errors.push(e.message));
      let configured=false,running=false,loggedIn=false,removed=false;
      const calls=[];
      const task={hash:'a'.repeat(40),name:'Ubuntu_测试文件_长名称用于验证窗口换行与布局.iso',size:2147483648,progress:.45,state:'downloading',dlspeed:2048000,upspeed:10240};
      await page.route('**/api/**',async route=>{
        const req=route.request(),url=new URL(req.url()),op=url.pathname.replace('/api/','');
        const data=req.method()==='POST'?req.postDataJSON():undefined;calls.push({op,data});
        let body={ok:true};
        if(op==='status') Object.assign(body,{configured,running,loggedIn,busy:false,error:'',directory:configured?'MiShare/qBDownloads':'',preview:true});
        else if(op==='browse') body.items=url.searchParams.get('path')?[]:[{name:'MiShare',path:'MiShare'}];
        else if(op==='service/setup'){configured=true;running=true;}
        else if(op==='service/stop')running=false;
        else if(op==='service/start')running=true;
        else if(op==='login')loggedIn=true;
        else if(op==='logout')loggedIn=false;
        else if(op==='torrents'){body.items=removed?[]:[task];body.transfer={dl_info_speed:2048000,up_info_speed:10240,dl_info_data:102400000};}
        else if(op==='stop')task.state='stoppedDL';
        else if(op==='start')task.state='downloading';
        else if(op==='remove')removed=true;
        else if(op==='limits' && req.method()==='GET')Object.assign(body,{download:0,upload:0,active:2});
        else if(op.startsWith('detail'))Object.assign(body,{files:[{name:task.name,size:task.size,progress:.45}],properties:{total_downloaded:12000,total_uploaded:1000,share_ratio:.08}});
        await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
      });
      await page.goto(process.env.QB_TEST_URL || 'http://127.0.0.1:18122/');
      await page.locator('#choose').click();
      await page.getByRole('button',{name:'MiShare',exact:true}).click();
      await page.locator('#selectFolder').click();
      await page.locator('#setupForm [name=password]').fill('Example123!');
      await page.locator('#setupForm [type=checkbox]').check();
      await page.locator('#setupForm [type=submit]').click();
      await page.locator('#loginForm [name=password]').fill('Example123!');
      await page.locator('#loginForm [type=submit]').click();
      await page.locator('.task').waitFor();
      await page.screenshot({path:path.join(out,`qa-qb-${width}.png`),fullPage:true});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'viewport overflow');
      assert.equal(await page.locator('img').evaluateAll(imgs=>imgs.some(i=>i.getBoundingClientRect().width>0&&(!i.complete||!i.naturalWidth))),false,'broken icon');
      await page.getByRole('button',{name:'暂停',exact:true}).click();
      await page.getByRole('button',{name:'继续',exact:true}).click();
      await page.locator('.task-name').click();
      await page.locator('#detailDialog[open]').waitFor();
      await page.locator('[data-close=detailDialog]').click();
      await page.locator('#limits').click();
      await page.locator('#limitForm [name=upload]').fill('128');
      await page.locator('#limitForm [type=submit]').click();
      await page.locator('#add').click();
      await page.locator('#addForm textarea').fill('magnet:?xt=urn:btih:'+'b'.repeat(40));
      await page.locator('#addForm [type=submit]').click();
      await page.locator('#addDialog').waitFor({state:'hidden'});
      await page.locator('#add').click();
      await page.locator('#addForm [type=file]').setInputFiles({name:'test.torrent',mimeType:'application/x-bittorrent',buffer:Buffer.from('d4:infodee')});
      await page.locator('#addForm [type=submit]').click();
      await page.locator('#addDialog').waitFor({state:'hidden'});
      await page.getByRole('button',{name:'移除任务，保留文件',exact:true}).click();
      await page.locator('#confirmRemove').click();
      await page.locator('#empty').waitFor();
      assert(calls.some(c=>c.op==='limits' && c.data?.upload===128));
      for(const op of ['magnet','torrent','start','stop','remove'])assert(calls.some(c=>c.op===op),op);
      assert.deepEqual(errors,[]);
      console.log(`PASS ${width}px: setup/login/browse/add/pause/resume/details/limits/remove; no overflow or broken icons`);
      await page.close();
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
