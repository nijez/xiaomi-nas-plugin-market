(() => {
  'use strict';
  const icons = __ICON_ASSETS__;
  const root = document.querySelector('#qb-app');
  const $ = (s) => root.querySelector(s);
  const session = document.querySelector('meta[name="qb-session"]').content;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = (name) => `<img src="${icons[name]}" alt="">`;
  root.querySelectorAll('[data-icon]').forEach(el => el.src = icons[el.dataset.icon]);
  let state, items = [], browsePath = '', removeHash, toastTimer, polling = false, generation = 0;
  const bytes = (n) => { n = Number(n || 0); const u = ['B','KiB','MiB','GiB','TiB']; let i = 0; while (n >= 1024 && i < 4) {n /= 1024;i++;} return n.toFixed(i ? 1 : 0) + ' ' + u[i]; };
  const stopped = item => /stopped|paused|error|missingFiles/i.test(item.state);
  function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 6500); }
  async function api(route, data) {
    const response = await fetch('api/' + route, {method:data === undefined ? 'GET':'POST', cache:'no-store', headers:{'X-QB-Session':session,'X-CSRF-Token':csrf,...(data === undefined ? {} : {'Content-Type':'application/json'})}, body:data === undefined ? undefined : JSON.stringify(data)});
    const result = await response.json(); if (!response.ok || !result.ok) throw new Error(result.error || '请求失败'); return result;
  }
  async function busy(button, fn) { button.disabled = true; try {await fn();} catch(e) {toast(e.message);} finally {button.disabled = false;} }
  function renderTasks() {
    const search = $('#search').value.toLowerCase(), filter = $('#filter').value;
    const visible = items.filter(item => item.name.toLowerCase().includes(search) && (filter === 'all' || filter === 'completed' && item.progress >= 1 || filter === 'stopped' && stopped(item) || filter === 'downloading' && item.progress < 1 && !stopped(item)));
    $('#count').textContent = `${visible.length} 个任务 · 最多显示最近 500 个`;
    $('#empty').hidden = visible.length > 0;
    $('#tasks').innerHTML = visible.map(item => `<article class="task"><div class="task-body"><button class="task-name" data-action="detail" data-hash="${escape(item.hash)}">${escape(item.name)}</button><progress max="1" value="${Math.max(0,Math.min(1,Number(item.progress)||0))}" aria-label="下载进度"></progress><small>${(item.progress*100).toFixed(1)}% · ${bytes(item.size)} · ${escape(item.state)} · ↓ ${bytes(item.dlspeed)}/s · ↑ ${bytes(item.upspeed)}/s</small></div><div class="task-actions"><button class="icon" title="${stopped(item)?'继续':'暂停'}" aria-label="${stopped(item)?'继续':'暂停'}" data-action="${stopped(item)?'start':'stop'}" data-hash="${escape(item.hash)}">${icon(stopped(item)?'play':'stop')}</button><button class="icon" title="移除任务，保留文件" aria-label="移除任务，保留文件" data-action="remove" data-hash="${escape(item.hash)}">${icon('trash')}</button></div></article>`).join('');
  }
  async function refresh() {
    if (polling) return; polling = true;
    try {
      state = await api('status');
      $('#setup').hidden = state.configured || state.busy;
      $('#login').hidden = !state.running || state.loggedIn || state.busy;
      $('#downloads').hidden = !state.running || !state.loggedIn;
      $('#serviceActions').hidden = !state.configured;
      $('#toggleService').disabled = state.busy;
      $('#toggleService').textContent = state.running ? '停止服务' : '启动服务';
      $('#serviceState').textContent = state.busy ? '正在处理，请稍候' : state.running ? '服务运行中' : state.preview ? '本机预览' : '服务未启动';
      $('#directory').textContent = state.directory ? '/' + state.directory : '';
      $('#error').hidden = !state.error; $('#error').textContent = state.error || '';
      if (state.running && state.loggedIn) {
        const result = await api('torrents'); items = result.items;
        $('#downSpeed').textContent = bytes(result.transfer.dl_info_speed) + '/s';
        $('#upSpeed').textContent = bytes(result.transfer.up_info_speed) + '/s';
        $('#downloaded').textContent = bytes(result.transfer.dl_info_data); renderTasks();
      }
    } catch(e) { $('#error').hidden = false; $('#error').textContent = e.message; }
    finally {polling = false;}
  }
  async function browse(path) {
    browsePath = path; const current = ++generation; $('#browsePath').textContent = '/' + path;
    $('#folders').textContent = '正在读取'; $('#selectFolder').disabled = true; $('#up').disabled = !path;
    try { const result = await api('browse?path=' + encodeURIComponent(path)); if(current !== generation) return;
      $('#folders').innerHTML = result.items.map(item => `<button type="button" data-folder="${escape(item.path)}">${icon('folder')}${escape(item.name)}</button>`).join('') || '<p class="muted">此目录下没有子文件夹</p>';
      $('#selectFolder').disabled = !path;
    } catch(e) { if(current === generation) $('#folders').textContent = e.message; }
  }
  root.addEventListener('click', e => {
    const button = e.target.closest('button'); if (!button) return;
    if (button.dataset.close) {$('#' + button.dataset.close).close();return;}
    if (button.dataset.folder !== undefined) {browse(button.dataset.folder);return;}
    if (!button.dataset.action) return;
    busy(button, async () => {
      const hash = button.dataset.hash, action = button.dataset.action;
      if (action === 'remove') {removeHash = hash; $('#removeDialog').showModal();return;}
      if (action === 'detail') {
        const result = await api('detail?hash=' + encodeURIComponent(hash));
        $('#detailTitle').textContent = items.find(x=>x.hash === hash)?.name || '任务详情';
        $('#detailStats').textContent = `已下载 ${bytes(result.properties.total_downloaded)} · 已上传 ${bytes(result.properties.total_uploaded)} · 分享率 ${Number(result.properties.share_ratio || 0).toFixed(2)}`;
        $('#files').innerHTML = result.files.map(file=>`<div class="file">${escape(file.name)}<small>${bytes(file.size)} · ${(file.progress*100).toFixed(1)}%</small></div>`).join('');
        $('#detailDialog').showModal(); return;
      }
      await api(action,{hash}); await refresh();
    });
  });
  $('#refresh').onclick = () => busy($('#refresh'),refresh);
  $('#search').oninput = renderTasks; $('#filter').onchange = renderTasks;
  $('#choose').onclick = () => {$('#browse').showModal();browse($('#downloadPath').value);};
  $('#up').onclick = () => browse(browsePath.split('/').slice(0,-1).join('/'));
  $('#selectFolder').onclick = () => {$('#downloadPath').value = browsePath; $('#browse').close();};
  $('#setupForm').onsubmit = e => {e.preventDefault();const f=e.target;if(!f.elements.path.value){toast('请先选择下载目录');return;}busy(f.querySelector('[type=submit]'),async()=>{await api('service/setup',{path:f.elements.path.value,password:f.elements.password.value}); f.elements.password.value='';await refresh();});};
  $('#loginForm').onsubmit = e => {e.preventDefault();const f=e.target;busy(f.querySelector('[type=submit]'),async()=>{await api('login',{password:f.elements.password.value});f.reset();await refresh();});};
  $('#toggleService').onclick = () => busy($('#toggleService'),async()=>{await api('service/' + (state.running?'stop':'start'),{});await refresh();});
  $('#add').onclick = () => {$('#addForm').reset();$('#addDialog').showModal();};
  $('#addForm').onsubmit = e => {e.preventDefault();const f=e.target;busy(f.querySelector('[type=submit]'),async()=>{
    const file=f.elements.torrent.files[0],magnet=f.elements.magnet.value.trim();
    if(!!file === !!magnet)throw new Error('请选择种子文件，或填写一条磁力链接');
    if(file){if(file.size>2*1024*1024)throw new Error('种子文件不能超过 2 MiB');const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('读取文件失败'));reader.readAsDataURL(file);});await api('torrent',{content});}
    else await api('magnet',{url:magnet});$('#addDialog').close();await refresh();toast('任务已添加');
  });};
  $('#limits').onclick = () => busy($('#limits'),async()=>{const limits=await api('limits');for(const k of ['download','upload','active'])$('#limitForm').elements[k].value=limits[k];$('#limitDialog').showModal();});
  $('#limitForm').onsubmit = e=>{e.preventDefault();busy(e.target.querySelector('[type=submit]'),async()=>{const f=e.target.elements;await api('limits',{download:Number(f.download.value),upload:Number(f.upload.value),active:Number(f.active.value)});$('#limitDialog').close();toast('已保存');});};
  $('#confirmRemove').onclick=()=>busy($('#confirmRemove'),async()=>{await api('remove',{hash:removeHash});$('#removeDialog').close();await refresh();});
  $('#logout').onclick=()=>busy($('#logout'),async()=>{await api('logout',{});await refresh();});
  async function tick(){if(!root.isConnected)return;if(!document.hidden)await refresh();setTimeout(tick,3000);}tick();
})();
