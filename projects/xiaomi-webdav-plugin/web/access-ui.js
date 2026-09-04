'use strict';
(() => {
  const control = (action, id, title, png) => `<button type="button" class="icon" data-access-action="${action}" data-key="${escapeHTML(id)}" title="${title}" aria-label="${title}" ${state.access.enabled?'disabled':''}>${icon(png)}</button>`;
  window.renderAccess = () => {
    const a = state.access;
    if (!a) return;
    $('#accessState').textContent = a.running ? '运行中' : a.enabled ? '未运行' : '已关闭';
    $('#toggleAccess').textContent = a.enabled ? '关闭多用户共享' : '开启多用户共享';
    const issues = [...a.issues, ...(a.error ? [a.error] : [])];
    $('#accessIssue').hidden = !issues.length;
    $('#accessIssue').textContent = issues.join('；');
    $('#accessLocked').hidden = !a.enabled;
    $('#accessCompatibility').textContent = a.issues.length ? '兼容性检查未通过，共享保持拒绝访问。' : '当前环境检查通过。固件、运行账号或授权目录发生变化时，停止共享并重新核对。';
    $('#accessUrls').textContent = a.running ? a.urls.join(' · ') : '';
    for (const id of ['addAccessFolder','addAccessUser','importLegacy','acceptHost']) $('#'+id).disabled=a.enabled;
    $('#importLegacy').disabled ||= !a.migration.available;
    $('#migrationPreview').textContent = a.migration.available ? `原账号 ${a.migration.username} · /${a.migration.path} · ${a.migration.permission==='read'?'只读':'读写'}；导入后连接路径为 /legacy/，不会自动开启。` : '独立权限配置；原单账号配置保持不变。';
    $('#accessFolders').innerHTML = a.folders.length ? a.folders.map(f=>`<article class="record"><div class="record-icon">${icon('folder')}</div><div class="record-info"><h3>${escapeHTML(f.name)}</h3><p>NAS /${escapeHTML(f.path)}</p><p>连接路径 /${escapeHTML(f.id)}/</p></div><div class="actions">${control('editFolder',f.id,'编辑目录','edit')}${control('removeFolder',f.id,'移除目录','trash')}</div></article>`).join('') : '<div class="empty"><h3>尚未授权任何目录</h3></div>';
    $('#accessUsers').innerHTML = a.users.length ? a.users.map(u=>`<article class="record"><div class="record-info"><h3>${escapeHTML(u.username)} <span class="status ${u.enabled?'live':''}">${u.enabled?'已启用':'已停用'}</span></h3><p>${Object.entries(u.grants).map(([id,mode])=>escapeHTML(a.folders.find(f=>f.id===id)?.name||id)+'：'+(mode==='read'?'只读':'读写')).join(' · ') || '没有目录访问权限'}</p></div><div class="actions">${control('editUser',u.username,'编辑账号权限','edit')}${control('removeUser',u.username,'移除账号','trash')}</div></article>`).join('') : '<div class="empty"><h3>尚未添加访问账号</h3></div>';
  };
  function openFolder(folder={}) {
    const form=$('#accessFolderForm');fill(form,folder);form.elements.id.readOnly=!!folder.id;
    $('#accessFolderDialog').showModal();
  }
  function openUser(user={enabled:true,grants:{}}) {
    const form=$('#accessUserForm');fill(form,user);form.elements.username.readOnly=!!user.username;
    form.elements.password.required=!user.hasPassword;
    form.elements.password.setCustomValidity('');
    $('#grantFields').innerHTML=state.access.folders.length ? state.access.folders.map(f=>`<label class="grant-row"><span>${escapeHTML(f.name)}<small>/${escapeHTML(f.path)}</small></span><select data-grant="${escapeHTML(f.id)}" aria-label="${escapeHTML(f.name)} 权限"><option value="none">不可访问</option><option value="read">只读</option><option value="write">读写</option></select></label>`).join('') : '<p class="muted">尚无共享目录，此账号暂不能访问文件。</p>';
    for(const el of form.querySelectorAll('[data-grant]'))el.value=user.grants[el.dataset.grant]||'none';
    $('#accessUserDialog').showModal();
  }
  $('#accessUserForm').elements.password.oninput = e => {
    const value=e.target.value;
    const valid=!value || (Array.from(value).length>=8 && Array.from(value).length<=200 && !/[\x00-\x1f\x7f]/.test(value) && [/[A-Z]/,/[a-z]/,/[0-9]/,/[!-/:-@\[-`{-~]/].filter(r=>r.test(value)).length>=2);
    e.target.setCustomValidity(valid?'':'密码须为 8 至 200 个字符，且包含至少两类字符');
  };
  $('#addAccessFolder').onclick=()=>openFolder();
  $('#addAccessUser').onclick=()=>openUser();
  $('#toggleAccess').onclick=()=>busy($('#toggleAccess'),()=>act('access/'+(state.access.enabled?'stop':'start'),{}));
  $('#acceptHost').onclick=()=>{if(confirm('确认固件和存储仍属于当前设备？只更新环境确认，不自动授权新目录或开启共享。'))busy($('#acceptHost'),()=>act('access/accept-host',{confirm:true}));};
  $('#importLegacy').onclick=()=>{if(confirm('导入当前账号、目录和权限？保留原配置，新连接路径改为 /legacy/，导入后仍保持关闭。'))busy($('#importLegacy'),()=>act('access/import-legacy',{confirm:true}));};
  for(const [id,route,dialog] of [['accessFolderForm','folder/save','accessFolderDialog'],['accessUserForm','user/save','accessUserDialog']]) {
    $('#'+id).onsubmit=event=>{
      event.preventDefault();const form=event.target;const body=fields(form);
      if(id==='accessUserForm')body.grants=Object.fromEntries([...form.querySelectorAll('[data-grant]')].filter(e=>e.value!=='none').map(e=>[e.dataset.grant,e.value]));
      busy(form.querySelector('[type=submit]'),async()=>{await act('access/'+route,body);form.elements.password&&(form.elements.password.value='');$('#'+dialog).close();toast('设置已保存');});
    };
  }
  document.addEventListener('click',event=>{
    const b=event.target.closest('[data-access-action]');if(!b||b.disabled)return;
    const id=b.dataset.key;
    if(b.dataset.accessAction==='editFolder')openFolder(state.access.folders.find(f=>f.id===id));
    if(b.dataset.accessAction==='editUser')openUser(state.access.users.find(u=>u.username===id));
    if(b.dataset.accessAction==='removeFolder'&&confirm('移除此目录及其账号授权？不会删除实际文件。'))busy(b,()=>act('access/folder/remove',{id}));
    if(b.dataset.accessAction==='removeUser'&&confirm('移除此 WebDAV 账号？不会删除文件或小米账号。'))busy(b,()=>act('access/user/remove',{username:id}));
  });
  if(state)window.renderAccess();
})();
