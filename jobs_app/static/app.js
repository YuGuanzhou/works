'use strict';

const $ = (id) => document.getElementById(id);
const PLATFORMS = {boss:{name:'BOSS 直聘',mark:'B',class:'boss',caption:'直接与招聘者沟通'},liepin:{name:'猎聘',mark:'猎',class:'liepin',caption:'发现专业职业机会'},'51job':{name:'前程无忧',mark:'51',class:'p51job',caption:'寻找适合你的岗位'},zhilian:{name:'智联招聘',mark:'智',class:'zhilian',caption:'连接更多职业可能'}};
const PAGES = {dashboard:'工作台',platforms:'平台配置',ai:'AI 与简历',jobs:'投递记录',blacklist:'企业黑名单',logs:'运行日志'};
const STATUS = {submitted:['已投递','good'],preview:['已预览','info'],filtered:['已过滤','warn'],failed:['未完成','bad'],unconfirmed:['待核实','warn'],already_contacted:['已沟通',''],submitting:['投递中','info'],pending:['等待中',''],running:['运行中','good'],stopping:['正在停止','warn'],completed:['已完成','good'],stopped:['已停止',''],error:['运行失败','bad']};
const SESSIONS = {closed:['未连接',''],open:['浏览器已打开','warn'],logged_in:['已登录','good'],login_required:['待登录','warn'],verification_required:['待验证','warn'],browser_blocked:['浏览器受限','bad'],unknown:['待检查','']};
let settings = null, dashboard = null, selectedPlatform = 'boss', currentPage = 'dashboard', jobPage = 1, jobsResult = {items:[],total:0,page_size:20}, logs = [], lastLogId = 0;
let settingsLoading = false, dashboardLoading = false, jobsLoading = false, logsLoading = false;
let lastDashboardError = '', jobRequestId = 0;

const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const platformName = (id) => PLATFORMS[id]?.name || (id === 'all' ? '全部平台' : id || '系统');
const formatTime = (value) => { if (!value) return '—'; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}); };
const splitList = (value) => value.split(/[,，\n;；]/).map(s => s.trim()).filter(Boolean);
const badge = (value, table = STATUS) => { const [label,color] = table[value] || [value || '未知','']; return `<span class="badge ${color}">${escapeHTML(label)}</span>`; };
const running = () => ['running','stopping','pending'].includes(dashboard?.task?.status) || dashboard?.platforms?.some(p => p.running);

function toast(message, type='success') {
  const element = document.createElement('div');
  element.className = `toast ${type}`;
  element.textContent = message;
  $('toasts').append(element);
  setTimeout(() => element.remove(), type === 'error' ? 7000 : 4200);
}

async function api(path, options={}) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== 'GET') headers.set('X-Jobs-Client','local-ui');
  if (options.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type','application/json');
    options.body = JSON.stringify(options.body);
  }
  let response;
  try { response = await fetch(path,{...options,headers}); }
  catch { throw new Error('无法连接本地服务，请确认 Python 后端正在运行。'); }
  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    let detail = data?.detail || data?.error || data?.message;
    if (Array.isArray(detail)) detail = detail.map(e => `${(e.loc || []).filter(s => s !== 'body').join('.')}: ${e.msg}`).join('；');
    if (typeof detail === 'object') detail = JSON.stringify(detail);
    throw new Error(detail || `请求失败（${response.status}），请查看运行日志。`);
  }
  return data;
}

async function action(button, callback) {
  if (button) { button.disabled = true; button.dataset.busy = 'true'; }
  try { return await callback(); }
  catch (error) { toast(error.message,'error'); }
  finally { if (button) { button.disabled = false; delete button.dataset.busy; } syncTaskControls(); }
}

function emptyState(title, message, colSpan, symbol='list') {
  const inner = `<div class="empty-icon">${icon(symbol)}</div><strong>${escapeHTML(title)}</strong><p>${escapeHTML(message)}</p>`;
  return colSpan ? `<tr><td colspan="${colSpan}" class="empty-state">${inner}</td></tr>` : `<div class="empty-state">${inner}</div>`;
}

function navigate() {
  const page = location.hash.slice(1).split('?')[0] || 'dashboard';
  currentPage = PAGES[page] ? page : 'dashboard';
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('hidden',p.id !== `page-${currentPage}`));
  document.querySelectorAll('.nav-item').forEach(a => {const active = a.dataset.page === currentPage; a.classList.toggle('active',active); active ? a.setAttribute('aria-current','page') : a.removeAttribute('aria-current');});
  $('breadcrumb-page').textContent = PAGES[currentPage];
  document.title = `${PAGES[currentPage]} · 工作无忧`;
  if (currentPage === 'jobs') loadJobs();
  if (currentPage === 'blacklist') loadBlacklist();
  if (currentPage === 'logs') loadLogs(true);
  if (['platforms','ai'].includes(currentPage) && !settings) loadSettings();
  if (currentPage === 'platforms') renderSession();
}

async function loadDashboard(silent=false) {
  if (dashboardLoading) return;
  dashboardLoading = true;
  try {
    dashboard = await api('/api/dashboard');
    $('connection').classList.remove('offline');
    $('connection').innerHTML = '<span class="status-dot"></span>本地服务已连接';
    $('service-error').classList.add('hidden'); lastDashboardError = '';
    renderDashboard();
  } catch (error) {
    $('connection').classList.add('offline');
    $('connection').innerHTML = '<span class="status-dot"></span>服务未连接';
    $('service-error').textContent = error.message;
    $('service-error').classList.remove('hidden');
    if (!silent && error.message !== lastDashboardError) toast(error.message,'error');
    lastDashboardError = error.message;
  } finally {dashboardLoading=false;}
}

function renderDashboard() {
  for (const key of ['submitted','preview','filtered','failed']) $('stat-'+key).textContent = Number(dashboard.today?.[key] || 0).toLocaleString();
  const source = dashboard.trend || [];
  const days = Array.from({length:7},(_,i) => {const date = new Date(); date.setDate(date.getDate()-6+i); const day = `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`; return {...(source.find(d => d.date?.slice(0,10) === day) || {}),date:day};});
  const max = Math.max(2,...days.flatMap(d => [Number(d.submitted||0),Number(d.preview||0)]));
  const any = days.some(d => d.submitted || d.preview);
  $('trend-chart').innerHTML = `<div class="chart-axis"><span>${max}</span><span>${Math.round(max/2)}</span><span>0</span></div>` + days.map(d => `<div class="chart-day" title="${escapeHTML(d.date)}：投递 ${d.submitted||0}，预览 ${d.preview||0}"><div class="chart-bar" style="height:${(d.submitted||0)/max*100}%"></div><div class="chart-bar preview" style="height:${(d.preview||0)/max*100}%"></div><span>${d.date.slice(5).replace('-','/')}</span></div>`).join('') + (any ? '' : '<span class="chart-empty">完成首次任务后，这里会展示你的求职足迹</span>');
  const task = dashboard.task;
  const isRunning = running();
  $('task-badge').textContent = task ? (STATUS[task.status]?.[0] || task.status) : '待命';
  $('task-status').classList.toggle('running',isRunning);
  $('task-status').innerHTML = icon(isRunning ? 'refresh' : 'clock') + `<span>${task ? `${escapeHTML(platformName(task.platform))} · ${task.mode === 'deliver'?'投递':'预览'}<br>${escapeHTML(task.message || '')}<br>已处理 ${Number(task.processed||0)} · 已投递 ${Number(task.submitted||0)} · 预览 ${Number(task.preview||0)} · 过滤 ${Number(task.filtered||0)} · 未完成 ${Number(task.failed||0)}` : '暂无任务，先登录平台并保存配置。'}</span>`;
  $('platform-cards').innerHTML = Object.entries(PLATFORMS).map(([id,p]) => { const session = dashboard.platforms?.find(s => s.id === id)?.session; return `<article class="platform-card"><div class="platform-card-top"><span class="platform-logo ${p.class}">${p.mark}</span><div><h3>${p.name}</h3><p>${p.caption}</p></div></div><div class="platform-card-bottom">${badge(session?.status || 'closed',SESSIONS)}<button class="row-action" data-open-platform="${id}">去配置 →</button></div></article>`; }).join('');
  renderSession(); syncTaskControls();
}

function syncTaskControls() {
  const active = running();
  for (const id of ['start-preview','start-deliver','task-platform','login-platform','logout-platform','save-platform','save-ai','delete-resume','resume-file']) {
    const element = $(id); if (element) element.disabled = active || element.dataset.busy === 'true' || (!settings && ['start-preview','start-deliver','save-platform','save-ai','delete-resume','resume-file'].includes(id));
  }
  document.querySelectorAll('#platform-form input, #platform-form textarea, #platform-form select, #ai-form input, #ai-form textarea, #ai-form select').forEach(element => {element.disabled = active || !settings;});
  $('pf-send-image').disabled = active || !settings || selectedPlatform !== 'boss';
  $('pf-greeting').disabled = active || !settings || selectedPlatform !== 'boss';
  $('start-preview').classList.toggle('hidden',active);
  $('start-deliver').classList.toggle('hidden',active);
  $('stop-task').classList.toggle('hidden',!active);
  $('stop-task').disabled = dashboard?.task?.status === 'stopping' || $('stop-task').dataset.busy === 'true';
}

async function loadSettings() {
  if (settingsLoading) return;
  settingsLoading = true;
  try {settings = await api('/api/settings'); renderPlatformForm(); renderAIForm(); syncTaskControls();}
  catch (error) {toast(error.message,'error');}
  finally {settingsLoading=false;}
}

function selectPlatform(id) {
  if (!PLATFORMS[id]) return;
  selectedPlatform = id;
  document.querySelectorAll('.platform-tab').forEach(b => {const active=b.dataset.platform===id;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active));});
  $('task-platform').value=id;
  renderPlatformForm(); renderSession();
}

function renderSession() {
  const p=PLATFORMS[selectedPlatform], session=dashboard?.platforms?.find(p => p.id === selectedPlatform)?.session;
  $('session-logo').textContent=p.mark; $('session-logo').className=`platform-logo ${p.class}`;
  $('session-title').textContent=p.name;
  const [label,color]=SESSIONS[session?.status] || SESSIONS.unknown;
  $('session-badge').className=`badge ${color}`; $('session-badge').textContent=label;
  $('session-description').textContent=session?.message || '点击「打开浏览器登录」，在浏览器中完成扫码或账号登录。';
}

function renderPlatformForm() {
  const config = settings?.platforms?.[selectedPlatform]; if (!config) return;
  const fields = {'keywords':'keywords','cities':'cities','city-codes':'city_codes','excluded':'excluded_words','inactive-statuses':'inactive_statuses'};
  for (const [id,key] of Object.entries(fields)) $('pf-'+id).value=(config[key] || []).join(', ');
  const numbers={'salary-min':'salary_min','salary-max':'salary_max','max-pages':'max_pages','daily-limit':'daily_limit','delay-min':'delay_min','delay-max':'delay_max'};
  for (const [id,key] of Object.entries(numbers)) $('pf-'+id).value=config[key] ?? ({max_pages:3,daily_limit:30,delay_min:3,delay_max:8}[key] || 0);
  for (const [id,key] of Object.entries({'inactive':'filter_inactive_hr','headhunters':'filter_headhunters','use-ai':'use_ai','send-image':'send_image'})) $('pf-'+id).checked=!!config[key];
  $('pf-greeting').value=config.greeting || '';
  if (selectedPlatform !== 'boss') $('pf-send-image').checked=false;
  $('pf-extra').value=JSON.stringify(config.extra_filters || {},null,2);
  $('platform-save-note').textContent=`正在编辑 ${platformName(selectedPlatform)} 的配置。`;
  syncTaskControls();
}

async function savePlatform(event) {
  event.preventDefault();
  const platform = selectedPlatform;
  await action($('save-platform'),async() => {
    let extra;try {extra=JSON.parse($('pf-extra').value || '{}');}catch {throw new Error('高级筛选参数必须是有效的 JSON 对象。');}
    if (!extra || Array.isArray(extra) || typeof extra !== 'object') throw new Error('高级筛选参数必须是 JSON 对象，例如 {}。');
    const config={...settings.platforms[platform]};
    for (const [id,key] of Object.entries({'keywords':'keywords','cities':'cities','city-codes':'city_codes','excluded':'excluded_words','inactive-statuses':'inactive_statuses'})) config[key]=splitList($('pf-'+id).value);
    for (const [id,key] of Object.entries({'salary-min':'salary_min','salary-max':'salary_max','max-pages':'max_pages','daily-limit':'daily_limit','delay-min':'delay_min','delay-max':'delay_max'})) config[key]=Number($('pf-'+id).value);
    for (const [id,key] of Object.entries({'inactive':'filter_inactive_hr','headhunters':'filter_headhunters','use-ai':'use_ai','send-image':'send_image'})) config[key]=$('pf-'+id).checked;
    if (!config.keywords.length) throw new Error('请至少填写一个职位关键词。');
    if (!config.cities.length) throw new Error('请至少填写一个期望城市。');
    if (config.city_codes.length && config.city_codes.length !== config.cities.length) throw new Error('城市代码需要与期望城市一一对应。');
    if (config.salary_max && config.salary_min>config.salary_max) throw new Error('最低月薪不能大于最高月薪。');
    if (config.delay_min>config.delay_max) throw new Error('最短操作间隔不能大于最长间隔。');
    config.greeting=$('pf-greeting').value.trim();config.extra_filters=extra;
    await api('/api/settings',{method:'PUT',body:{platforms:{[platform]:config}}});
    settings.platforms[platform]=config;
    toast(`${platformName(platform)} 配置已保存`);
    if (selectedPlatform === platform) $('platform-save-note').textContent=`已保存 · ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;
  });
}

function renderAIForm() {
  const ai=settings?.ai || {}, notifications=settings?.notifications || {};
  $('ai-enabled').checked=!!ai.enabled;$('ai-base-url').value=ai.base_url || '';$('ai-model').value=ai.model || '';$('ai-min-score').value=ai.min_score ?? 60;
  $('ai-profile').value=ai.profile || '';$('ai-prompt').value=ai.prompt || '';$('ai-key').value='';$('ai-clear-key').checked=false;
  $('ai-key-state').textContent=ai.api_key_set ? '已保存' : '尚未设置';
  $('notifications-enabled').checked=!!notifications.enabled;$('notifications-webhook').value='';$('notifications-clear').checked=false;
  $('webhook-state').textContent=notifications.webhook_set?'已保存':'尚未设置';renderResume();
}

function renderResume() {
  const resume=settings?.resume || {};
  $('resume-state').textContent=resume.has_image ? `已上传：${resume.filename || '简历图片'}` : '尚未上传图片简历';
  $('delete-resume').classList.toggle('hidden',!resume.has_image);
}

async function saveAI(event) {
  event.preventDefault();
  await action($('save-ai'),async() => {
    const ai={enabled:$('ai-enabled').checked,base_url:$('ai-base-url').value.trim(),model:$('ai-model').value.trim(),api_key:$('ai-key').value.trim(),clear_api_key:$('ai-clear-key').checked,profile:$('ai-profile').value.trim(),prompt:$('ai-prompt').value.trim(),min_score:Number($('ai-min-score').value)};
    const notifications={enabled:$('notifications-enabled').checked,webhook:$('notifications-webhook').value.trim(),clear_webhook:$('notifications-clear').checked};
    await api('/api/settings',{method:'PUT',body:{ai,notifications}});
    const updated=await api('/api/settings');settings.ai=updated.ai;settings.notifications=updated.notifications;
    renderAIForm();toast('AI 与通知配置已保存');
  });
}

async function platformAction(kind) {
  if (kind === 'logout' && !window.confirm(`确认退出「${platformName(selectedPlatform)}」？\n\n此操作会关闭平台浏览器并清除本机保存的登录状态。下次使用需要重新登录。`)) return;
  const button=$(kind==='login'?'login-platform':kind==='logout'?'logout-platform':'check-session');
  await action(button,async() => {
    const result=await api(`/api/platforms/${selectedPlatform}/${kind}`,kind==='status'?{}:{method:'POST'});
    if (kind==='login') toast(result.message || '浏览器已打开，请在浏览器中完成登录。','info');
    if (kind==='logout') toast('已退出登录，并清除保存的登录状态');
    await loadDashboard(true);
    if (kind==='status') toast('登录状态已更新');
  });
}

async function startTask(mode) {
  const platform=$('task-platform').value;
  if (mode==='deliver' && !window.confirm(`确认在「${platformName(platform)}」开始实际投递？\n\n程序会使用已保存的配置向招聘者发送招呼语或提交职位申请；若已开启图片简历，也可能发送已上传的简历。请确认内容正确。\n\n点击“确定”开始投递。`)) return;
  await action($(mode==='deliver'?'start-deliver':'start-preview'),async() => {
    await api('/api/tasks',{method:'POST',body:{platform,mode}});
    toast(mode==='deliver'?'投递任务已启动':'匹配预览已启动，不会向企业发送消息');
    await loadDashboard(true);
  });
}

function jobQuery() {return new URLSearchParams({platform:$('job-platform').value,status:$('job-status').value,q:$('job-query').value.trim(),page:String(jobPage),page_size:'20'});}

async function loadJobs() {
  const requestId=++jobRequestId;
  jobsLoading=true;
  $('jobs-total').textContent='正在读取记录…';
  try {
    const data=await api('/api/jobs?'+jobQuery());if (requestId!==jobRequestId) return;jobsResult=data;
    const items=data.items || [];
    $('jobs-body').innerHTML=items.length?items.map(j => `<tr><td><span class="job-title">${escapeHTML(j.title || '未命名职位')}</span><span class="job-subtitle">${escapeHTML(j.company || '企业名称未知')}</span></td><td>${escapeHTML(platformName(j.platform))}</td><td><span class="job-salary">${escapeHTML(j.salary || '薪资未标注')}</span><span class="job-city">${escapeHTML(j.city || '城市未标注')}</span></td><td>${j.score === null || j.score === undefined?'<span class="job-subtitle">—</span>':`<span class="job-score">${escapeHTML(j.score)}</span>`}</td><td>${badge(j.status)}</td><td>${escapeHTML(formatTime(j.created_at))}</td><td><button class="row-action" data-job-detail="${escapeHTML(j.id)}">查看详情</button></td></tr>`).join(''):emptyState('这里还没有记录','开始一次匹配预览，合适的机会将在这里出现。',7);
    const pages=Math.max(1,Math.ceil((data.total || 0)/(data.page_size || 20)));
    $('jobs-total').textContent=`共 ${Number(data.total || 0).toLocaleString()} 条记录`;$('jobs-page').textContent=`${jobPage} / ${pages}`;
    $('jobs-prev').disabled=jobPage<=1;$('jobs-next').disabled=jobPage>=pages;
  }catch(error){if(requestId!==jobRequestId)return;toast(error.message,'error');$('jobs-total').textContent='读取失败，请重新查询';$('jobs-body').innerHTML=emptyState('暂时无法读取记录',error.message,7,'alert');}
  finally{if(requestId===jobRequestId)jobsLoading=false;}
}

function showJobDetail(id) {
  const j=jobsResult.items.find(item=>String(item.id)===String(id));if(!j)return;
  let safeUrl='';try{const url=new URL(j.url);if(['https:','http:'].includes(url.protocol))safeUrl=url.href;}catch{}
  $('job-detail').innerHTML=`<div class="job-detail-content"><h3>${escapeHTML(j.title)}</h3><div class="detail-meta"><span>${escapeHTML(j.company)}</span><span>${escapeHTML(platformName(j.platform))}</span>${badge(j.status)}</div><div class="detail-meta"><span>${escapeHTML(j.salary || '薪资未标注')}</span><span>${escapeHTML(j.city || '城市未标注')}</span><span>${escapeHTML(formatTime(j.created_at))}</span>${j.score===null||j.score===undefined?'':`<span>匹配度 ${escapeHTML(j.score)} / 100</span>`}</div><div class="detail-block"><label>职位描述</label><p>${escapeHTML(j.description || '暂无职位描述')}</p></div><div class="detail-block"><label>处理说明</label><p>${escapeHTML(j.reason || '暂无说明')}</p></div><div class="detail-block"><label>招呼语</label><p>${escapeHTML(j.greeting || '尚未生成招呼语')}</p></div><div class="detail-actions">${safeUrl?`<a class="button secondary" href="${escapeHTML(safeUrl)}" target="_blank" rel="noopener noreferrer">查看原始职位 ${icon('arrow')}</a>`:''}<button class="button subtle" data-blacklist-job="${escapeHTML(j.id)}">将企业加入黑名单</button></div></div>`;
  $('job-dialog').showModal();
}

async function loadBlacklist() {
  try {
    const data=await api('/api/blacklist');const items=data.items || [];
    $('blacklist-count').textContent=`${items.length} 家企业`;
    $('blacklist-body').innerHTML=items.length?items.map(item=>`<tr><td class="wrap-cell"><span class="job-title">${escapeHTML(item.company)}</span></td><td>${escapeHTML(platformName(item.platform))}</td><td class="wrap-cell">${escapeHTML(item.reason || '—')}</td><td>${escapeHTML(formatTime(item.created_at))}</td><td><button class="row-action delete" data-delete-blacklist="${escapeHTML(item.id)}">移除</button></td></tr>`).join(''):emptyState('暂时没有需要跳过的企业','可以在上方添加，或从职位详情中直接加入。',5,'shield');
  }catch(error){toast(error.message,'error');$('blacklist-body').innerHTML=emptyState('暂时无法读取黑名单',error.message,5,'alert');}
}

async function loadLogs(reset=false) {
  if(logsLoading)return;logsLoading=true;
  if(reset){lastLogId=0;logs=[];}
  const level=$('log-level').value,platform=$('log-platform').value;
  try{
    const data=await api('/api/logs?'+new URLSearchParams({after:String(lastLogId),level,platform}));
    if(level!==$('log-level').value||platform!==$('log-platform').value){logsLoading=false;loadLogs(true);return;}
    const existing=new Set(logs.map(l=>l.id));const incoming=(data.items || []).filter(l=>!existing.has(l.id));
    logs=[...logs,...incoming].sort((a,b)=>Number(a.id)-Number(b.id)).slice(-500);
    if(logs.length)lastLogId=Math.max(...logs.map(l=>Number(l.id)));
    const panel=$('logs-list'),nearBottom=panel.scrollHeight-panel.scrollTop-panel.clientHeight<80;
    panel.innerHTML=logs.length?logs.map(log=>`<div class="log-row"><time class="log-time">${escapeHTML(formatTime(log.created_at))}</time><span class="log-level ${String(log.level).toLowerCase()}">${escapeHTML(({INFO:'信息',WARNING:'警告',ERROR:'错误',DEBUG:'调试'}[String(log.level).toUpperCase()] || log.level))}</span><span class="log-platform">${escapeHTML(platformName(log.platform))}</span><span class="log-message">${escapeHTML(log.message)}</span></div>`).join(''):emptyState('当前没有日志','任务启动后，运行进度与结果会显示在这里。',0,'terminal');
    $('logs-count').textContent=`显示 ${logs.length} 条日志`;
    if(nearBottom||reset)panel.scrollTop=panel.scrollHeight;
  }catch(error){if(reset){toast(error.message,'error');$('logs-count').textContent='日志读取失败';}}
  finally{logsLoading=false;}
}

function bindEvents() {
  window.addEventListener('hashchange',navigate);
  $('refresh-dashboard').addEventListener('click',()=>action($('refresh-dashboard'),()=>loadDashboard()));
  $('platform-tabs').addEventListener('click',event=>{const tab=event.target.closest('[data-platform]');if(tab)selectPlatform(tab.dataset.platform);});
  $('platform-cards').addEventListener('click',event=>{const button=event.target.closest('[data-open-platform]');if(button){selectPlatform(button.dataset.openPlatform);location.hash='platforms';}});
  $('platform-form').addEventListener('submit',savePlatform);
  $('ai-form').addEventListener('submit',saveAI);
  $('login-platform').addEventListener('click',()=>platformAction('login'));
  $('logout-platform').addEventListener('click',()=>platformAction('logout'));
  $('check-session').addEventListener('click',()=>platformAction('status'));
  $('start-preview').addEventListener('click',()=>startTask('preview'));
  $('start-deliver').addEventListener('click',()=>startTask('deliver'));
  $('stop-task').addEventListener('click',()=>action($('stop-task'),async()=>{await api('/api/tasks/stop',{method:'POST'});toast('已请求停止，当前步骤结束后退出。','info');await loadDashboard(true);}));
  $('ai-test-form').addEventListener('submit',event=>{event.preventDefault();action($('test-ai'),async()=>{const result=await api('/api/ai/test',{method:'POST',body:{job_description:$('test-description').value.trim()}});$('ai-test-result').classList.remove('hidden');$('ai-test-result').innerHTML=`<div class="test-result"><div class="test-score">${escapeHTML(result.score)}<span>/ 100 匹配分数</span></div><h3>匹配分析</h3><p>${escapeHTML(result.reason || '暂无说明')}</p><h3>建议招呼语</h3><p>${escapeHTML(result.greeting || '尚未生成')}</p></div>`;toast('AI 匹配测试完成');});});
  $('resume-file').addEventListener('change',()=>action($('resume-file'),async()=>{const file=$('resume-file').files[0];if(!file)return;const data=new FormData();data.append('file',file);await api('/api/resume',{method:'POST',body:data});settings.resume=(await api('/api/settings')).resume;renderResume();toast('图片简历已上传');$('resume-file').value='';}));
  $('delete-resume').addEventListener('click',()=>action($('delete-resume'),async()=>{await api('/api/resume',{method:'DELETE'});settings.resume={has_image:false,filename:''};renderResume();toast('图片简历已删除');}));
  $('job-filter-form').addEventListener('submit',event=>{event.preventDefault();jobPage=1;loadJobs();});
  $('jobs-prev').addEventListener('click',()=>{if(jobPage>1&&!jobsLoading){jobPage--;loadJobs();}});
  $('jobs-next').addEventListener('click',()=>{if(!jobsLoading){jobPage++;loadJobs();}});
  $('export-jobs').addEventListener('click',()=>{const query=jobQuery();query.delete('page');query.delete('page_size');window.location.assign('/api/jobs/export?'+query);});
  $('jobs-body').addEventListener('click',event=>{const button=event.target.closest('[data-job-detail]');if(button)showJobDetail(button.dataset.jobDetail);});
  $('close-job-dialog').addEventListener('click',()=>$('job-dialog').close());
  $('job-dialog').addEventListener('click',event=>{if(event.target===$('job-dialog'))$('job-dialog').close();const button=event.target.closest('[data-blacklist-job]');if(button)action(button,async()=>{await api(`/api/jobs/${encodeURIComponent(button.dataset.blacklistJob)}/blacklist`,{method:'POST'});toast('已加入企业黑名单，后续任务会自动过滤');button.removeAttribute('data-blacklist-job');button.textContent='已加入黑名单';});});
  $('blacklist-form').addEventListener('submit',event=>{event.preventDefault();action($('add-blacklist'),async()=>{await api('/api/blacklist',{method:'POST',body:{company:$('blacklist-company').value.trim(),platform:$('blacklist-platform').value,reason:$('blacklist-reason').value.trim()}});$('blacklist-company').value='';$('blacklist-reason').value='';toast('企业已加入黑名单');await loadBlacklist();});});
  $('blacklist-body').addEventListener('click',event=>{const button=event.target.closest('[data-delete-blacklist]');if(button)action(button,async()=>{await api(`/api/blacklist/${encodeURIComponent(button.dataset.deleteBlacklist)}`,{method:'DELETE'});toast('企业已移出黑名单');await loadBlacklist();});});
  $('refresh-logs').addEventListener('click',()=>action($('refresh-logs'),()=>loadLogs(true)));
  $('log-level').addEventListener('change',()=>loadLogs(true));$('log-platform').addEventListener('change',()=>loadLogs(true));
  $('log-auto').addEventListener('change',()=>{if($('log-auto').checked)loadLogs();});
}

function init() {
  $('today-label').textContent=new Date().toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});
  $('platform-tabs').innerHTML=Object.entries(PLATFORMS).map(([id,p],index)=>`<button class="platform-tab ${index===0?'active':''}" data-platform="${id}" role="tab" aria-selected="${index===0}"><span class="platform-logo ${p.class}">${p.mark}</span>${p.name}</button>`).join('');
  $('jobs-body').innerHTML=emptyState('正在读取记录','请稍候…',7);
  $('blacklist-body').innerHTML=emptyState('正在读取企业','请稍候…',5,'shield');
  bindEvents();navigate();syncTaskControls();loadSettings();loadDashboard(true);
  setInterval(()=>{if(!document.hidden){loadDashboard(true);if(currentPage==='logs'&&$('log-auto').checked)loadLogs();}},4000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadDashboard(true);});
}
init();

