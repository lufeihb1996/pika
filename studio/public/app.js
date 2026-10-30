'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {
  auth: false,
  online: false,
  jobs: [],
  selected: null,
  uploads: {},
  refs: [],
  uploading: 0,
  polling: false,
  savedURLs: [],
  tab: 'create',
  filter: 'all',
  monitorCollapsed: true
};

const labels = {
  draft: '草稿',
  queued: '排队中',
  running: '生成中',
  cancelling: '正在取消',
  cancelled: '已取消',
  done: '已完成',
  failed: '生成失败',
  interrupted: '已中断'
};

const modeMeta = {
  t2va: { title: 'T2VA · 文生视频', tip: '<strong>T2VA 文生视频</strong>：纯文本直接生成音画同步视频，无需上传图片，支持引号台词、环境音与配乐描述。' },
  i2va: { title: 'I2VA · 首帧图生', tip: '<strong>I2VA 首帧图生</strong>：上传 1 张首帧图片作为镜头起点，按提示词驱动人物动作、运镜与声音。' },
  l2va: { title: 'L2VA · 尾帧引导', tip: '<strong>L2VA 尾帧引导</strong>：上传 1 张目标尾帧图片，镜头动作自然过渡并落幅在指定画面。' },
  fl2va: { title: 'FL2VA · 首尾接续', tip: '<strong>FL2VA 首尾帧接续</strong>：同时上传首帧与尾帧图片，平滑连接两个关键瞬间并同步生成音效。' },
  ref2va: { title: 'REF2VA · 多参考融合', tip: '<strong>REF2VA 多参考融合</strong>：最多上传 9 图 / 3 视频 / 3 音频（共 12 个），融合多模态参考生成音画镜头。' }
};

const directorPresets = [
  {
    name: '🎬 电影感人像特写对白',
    task: 't2va',
    aspect: '16:9',
    duration: 5,
    prompt: 'Cinematic medium close-up shot of a young woman standing by a rain-streaked cafe window at dusk, warm neon reflections on glass. She turns slightly toward the camera with a gentle smile and says softly, "We finally made it here." Audio: warm female voice, soft rain tapping on window, mellow lo-fi jazz piano in background.'
  },
  {
    name: '⚡ 赛博雨夜高速追逐',
    task: 't2va',
    aspect: '16:9',
    duration: 5,
    prompt: 'Dynamic tracking shot following a sleek futuristic motorcycle weaving through neon-lit cyberpunk city streets in heavy rain. Water splashes off the tires as the camera pans with large amplitude at fast speed. Audio: roaring electric turbine engine, tire screeches on wet asphalt, thunder rumble and synthwave bass.'
  },
  {
    name: '🌊 海岸日落空镜与环境音',
    task: 't2va',
    aspect: '16:9',
    duration: 5,
    prompt: 'Wide aerial drone shot gliding slowly over turquoise ocean waves crashing against dark volcanic cliffs at golden hour sunset. Sea foam sparkles in warm sunlight, seagulls flying across the horizon. Audio: immersive ocean waves crashing, distant seagull calls, gentle coastal wind.'
  },
  {
    name: '📱 竖屏真实感街拍口播',
    task: 'i2va',
    aspect: '9:16',
    duration: 5,
    prompt: 'Handheld smartphone vlog style medium shot. The subject in the first frame looks naturally into the lens while walking along a sunlit tree-lined street, gesturing casually and speaking with lively expression. Audio: clear natural voice with subtle outdoor street ambiance and footsteps.'
  },
  {
    name: '🔗 多参考角色与场景融合',
    task: 'ref2va',
    aspect: '16:9',
    duration: 5,
    prompt: 'Medium shot combining the character and environment from references. The protagonist steps forward under dramatic rim lighting, coat fluttering in the wind, looking determined. Audio: cinematic orchestral swell, wind blowing, crisp footsteps on stone.'
  }
];

let toastTimer;
function toast(message) {
  $('toast').textContent = message;
  $('toast').classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').classList.remove('visible'), 4000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: {
      ...(options.body && typeof options.body === 'string' ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers
    }
  });
  const data = await response.json().catch(() => ({ error: `服务返回 HTTP ${response.status}` }));
  if (response.status === 401) {
    state.auth = false;
    throw new Error('Colab 正在运行旧版或受密码保护的服务，请重新运行工作台启动脚本');
  }
  if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
  return data;
}

function setConnection(text, mode = '') {
  $('connection').className = 'connection health-label ' + mode;
  $('connection').innerHTML = '<i></i>' + esc(text);
  const dot = $('cloudDot');
  if (dot) dot.className = 'status-pulse ' + (mode === 'online' ? 'online' : mode === 'offline' ? 'offline' : '');
}

function tab(name) {
  state.tab = name;
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('selected', b.dataset.tab === name));
  for (const t of ['create', 'history', 'logs']) {
    const panel = $(t + '-panel');
    if (panel) panel.hidden = t !== name;
  }
  if (name === 'history') renderHistory();
  if (name === 'logs') loadLog();
}
document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => tab(b.dataset.tab)));

for (let i = 5; i <= 15; i++) {
  $('duration').add(new Option(`${i} 秒${i === 5 ? ' · 首次推荐' : ''}`, String(i)));
}

function renderPresets() {
  const box = $('presetList');
  if (!box) return;
  box.innerHTML = directorPresets.map((p, idx) => `
    <button type="button" class="nav-item preset-item" data-preset="${idx}">
      <div class="preset-item-top">
        <span class="preset-item-title">${esc(p.name)}</span>
        <span class="mode-badge ${esc(p.task)}">${esc(p.task.toUpperCase())}</span>
      </div>
      <div class="preset-item-desc">${esc(p.prompt)}</div>
    </button>
  `).join('');
  box.querySelectorAll('[data-preset]').forEach(btn => {
    btn.onclick = () => {
      const p = directorPresets[Number(btn.dataset.preset)];
      if (!p) return;
      $('task').value = p.task;
      renderInputs();
      $('aspect').value = p.aspect;
      $('duration').value = String(p.duration);
      $('prompt').value = p.prompt;
      syncSettings();
      promptCount();
      toast(`已载入预设「${p.name}」`);
    };
  });
}
renderPresets();

// 官方实测：480p 任意时长、768p 5 秒都能在不卸载的默认模式下跑完（H100 采样约 66–69 GiB）；
// 768p 15 秒在 H100 默认模式就会 OOM，必须加 --cpu-offload。A100 80GB 沿用同一档位。
function recommendedOffload(resolution, duration) {
  return resolution === '768p' && Number(duration) > 5;
}

function offloadNote() {
  $('render-note').textContent = $('offload').checked
    ? '已开启 CPU 卸载：显存占用降低，但每次前向都要搬权重，速度明显变慢。'
    : '已关闭 CPU 卸载：权重常驻显存，速度更快；显存不足时再开启。';
}

function syncSettings(auto) {
  const currentTask = $('task').value;
  document.querySelectorAll('[data-aspect]').forEach(b => b.classList.toggle('selected', b.dataset.aspect === $('aspect').value));
  document.querySelectorAll('[data-mode]').forEach(b => b.classList.toggle('active', b.dataset.mode === currentTask));
  const info = modeMeta[currentTask] || modeMeta.t2va;
  if ($('inspectorModeTitle')) $('inspectorModeTitle').textContent = info.title;
  if ($('modeTip')) $('modeTip').innerHTML = info.tip;
  if (auto) $('offload').checked = recommendedOffload($('resolution').value, $('duration').value);
  offloadNote();
}

document.querySelectorAll('[data-aspect]').forEach(b => b.addEventListener('click', () => {
  $('aspect').value = b.dataset.aspect;
  syncSettings();
  draft();
}));
document.querySelectorAll('[data-mode]').forEach(b => b.addEventListener('click', () => {
  $('task').value = b.dataset.mode;
  renderInputs();
  syncSettings();
}));
$('aspect').addEventListener('change', syncSettings);
$('resolution').addEventListener('change', () => syncSettings(true));

function draft() {
  try {
    localStorage.setItem('pika-draft', JSON.stringify({
      prompt: $('prompt').value,
      resolution: $('resolution').value,
      duration: $('duration').value,
      aspect: $('aspect').value,
      seed: $('seed').value
    }));
  } catch {}
}
try {
  const d = JSON.parse(localStorage.getItem('pika-draft') || 'null');
  if (d) {
    for (const k of ['prompt', 'resolution', 'duration', 'aspect', 'seed']) {
      if (d[k] != null) $(k).value = d[k];
    }
  }
} catch {}

function promptCount() {
  $('char-count').textContent = `${$('prompt').value.length} / 8000`;
  draft();
}
$('prompt').addEventListener('input', promptCount);
for (const k of ['resolution', 'duration', 'aspect', 'seed']) $(k).addEventListener('change', draft);
$('duration').addEventListener('change', () => syncSettings(true));
$('offload').addEventListener('change', offloadNote);
promptCount();

function resetComposer() {
  $('task').value = 't2va';
  $('prompt').value = '';
  $('resolution').value = '480p';
  $('duration').value = '5';
  $('aspect').value = '16:9';
  $('seed').value = '';
  $('offload').checked = recommendedOffload('480p', 5);
  renderInputs();
  syncSettings();
  promptCount();
  toast('已重置为空白镜头配置');
}
if ($('resetComposerBtn')) $('resetComposerBtn').onclick = resetComposer;
if ($('newBlankShotBtn')) $('newBlankShotBtn').onclick = newShot;
if ($('addShotBtn')) $('addShotBtn').onclick = newShot;
if ($('saveShotBtn')) $('saveShotBtn').onclick = saveShot;
if ($('seedRandBtn')) $('seedRandBtn').onclick = () => { $('seed').value = ''; draft(); };
if ($('seed42Btn')) $('seed42Btn').onclick = () => { $('seed').value = '42'; draft(); };

function inspectorParams(){
  return {task:$('task').value,prompt:$('prompt').value,resolution:$('resolution').value,
    duration:Number($('duration').value),aspect:$('aspect').value,seed:$('seed').value===''?null:Number($('seed').value),
    cpu_offload:$('offload').checked,first_image:state.uploads.first_image||'',last_image:state.uploads.last_image||'',
    references:$('task').value==='ref2va'?state.refs.map(({id,kind})=>({id,kind})):[]};
}
function loadInspector(job){
  if(!job)return;
  const p=job.editor_params||job.params||{};
  state.inspectorJob=job.id;
  $('task').value=p.task||'t2va';renderInputs();
  for(const k of ['prompt','resolution','duration','aspect','seed'])$(k).value=p[k]??(k==='seed'?'':({resolution:'480p',duration:5,aspect:'16:9',prompt:''}[k]));
  $('offload').checked=p.cpu_offload!==false;
  state.uploads={first_image:p.first_image||'',last_image:p.last_image||''};
  state.refs=(p.references||[]).map((r,i)=>({...r,name:`已上传参考素材 ${i+1}`}));renderRefs();
  for(const key of ['first_image','last_image'])if(state.uploads[key]&&$('drop-'+key))$('drop-'+key).querySelector('b').textContent='已关联素材（点击可替换）';
  const frozen=['running','queued','cancelling'].includes(job.status);
  $('saveShotBtn').disabled=frozen;
  $('inspectorStatusBadge').textContent=job.status==='draft'?'草稿':frozen?'参数已冻结':'编辑副本';
  syncSettings();promptCount();
}
async function newShot(){
  try{
    const job=await api('/api/local/shots',{method:'POST',body:'{}'});
    state.jobs=[job,...state.jobs];state.filter='all';
    document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter==='all'));
    tab('create');selectJob(job);toast('已新建镜头，填写右侧参数后保存或生成');
  }catch(e){toast(e.message);}
}
async function saveShot(){
  try{
    const id=state.inspectorJob;
    const job=await api(id?`/api/local/shots/${id}/save`:'/api/local/shots',{method:'POST',body:JSON.stringify(inspectorParams())});
    state.inspectorJob=job.id;state.selected=job.id;
    state.jobs=[job,...state.jobs.filter(j=>j.id!==job.id)];
    renderCurrent();renderHistory();toast('镜头参数已保存到本地');
  }catch(e){toast(e.message);}
}
async function deleteShot(job){
  try{
    await api(`/api/local/shots/${job.id}/delete`,{method:'POST',body:'{}'});
    state.jobs=state.jobs.filter(j=>j.id!==job.id);
    if(state.selected===job.id){state.selected=state.jobs[0]?.id;state.inspectorJob=null;if(state.jobs[0])loadInspector(state.jobs[0]);else resetComposer();}
    renderCurrent();renderHistory();toast('已移出列表，视频文件保留在 outputs');
  }catch(e){toast(e.message);}
}

function renderInputs() {
  const task = $('task').value;
  state.uploads = {};
  state.refs = [];
  $('reference-list').replaceChildren();
  $('reference-inputs').hidden = task !== 'ref2va';
  if ($('t2va-ref-notice')) $('t2va-ref-notice').hidden = task !== 't2va';
  const keys = { t2va: [], i2va: ['first_image'], l2va: ['last_image'], fl2va: ['first_image', 'last_image'], ref2va: [] }[task] || [];
  $('endpoint-inputs').innerHTML = keys.map(k => `
    <div>
      <label class="form-label" style="margin-bottom:5px;display:block">${k === 'first_image' ? 'First Frame · 首帧图片' : 'Last Frame · 尾帧图片'}</label>
      <label class="drop-zone" id="drop-${k}">
        <span>＋</span>
        <b>点击上传图片</b>
        <small>PNG / JPG / WEBP</small>
        <input type="file" accept="image/png,image/jpeg,image/webp" hidden data-upload="${k}">
      </label>
    </div>
  `).join('');
  document.querySelectorAll('[data-upload]').forEach(input => input.addEventListener('change', async () => {
    if (!input.files[0]) return;
    const key = input.dataset.upload;
    const file = input.files[0];
    delete state.uploads[key];
    try {
      const result = await upload(file);
      const zone = $('drop-' + key);
      if (!zone) return;
      state.uploads[key] = result.id;
      zone.querySelector('b').textContent = file.name;
      const img = new Image();
      img.alt = file.name;
      img.src = URL.createObjectURL(file);
      img.onload = () => URL.revokeObjectURL(img.src);
      (zone.querySelector('img') || zone.querySelector('span')).replaceWith(img);
    } catch (e) {
      toast(e.message);
    } finally {
      input.value = '';
    }
  }));
  syncSettings();
}
$('task').addEventListener('change', renderInputs);
renderInputs();

async function upload(file) {
  if (!state.auth) throw new Error('Colab 后端未就绪，请检查连接');
  if (file.size > 200 * 1024 * 1024) throw new Error('每个素材上限为 200MiB');
  state.uploading++;
  $('generate').disabled = true;
  if ($('generateBottomBtn')) $('generateBottomBtn').disabled = true;
  toast(`正在上传 ${file.name}…`);
  try {
    return await api('/api/uploads?name=' + encodeURIComponent(file.name), {
      method: 'POST',
      body: file,
      headers: { 'Content-Type': 'application/octet-stream' }
    });
  } finally {
    state.uploading--;
    const busy = state.uploading > 0;
    $('generate').disabled = busy;
    if ($('generateBottomBtn')) $('generateBottomBtn').disabled = busy;
  }
}

function renderRefs() {
  $('reference-list').innerHTML = state.refs.map((ref, i) => `
    <div class="reference-item">
      <b>#${i + 1}</b>
      <span>${esc(ref.kind)} · ${esc(ref.name)}</span>
      <button type="button" data-remove="${i}" aria-label="移除参考素材">×</button>
    </div>
  `).join('');
  $('reference-list').querySelectorAll('[data-remove]').forEach(b => b.onclick = () => {
    state.refs.splice(Number(b.dataset.remove), 1);
    renderRefs();
  });
}

$('reference-files').addEventListener('change', async event => {
  for (const file of event.target.files) {
    if (state.refs.length >= 12) {
      toast('最多 12 个参考素材');
      break;
    }
    try {
      state.refs.push(await upload(file));
      renderRefs();
    } catch (e) {
      toast(e.message);
    }
  }
  event.target.value = '';
});

if ($('quickSubmitBtn')) {
  $('quickSubmitBtn').onclick = () => {
    if (!$('prompt').value.trim()) {
      toast('请先在右侧填写镜头提示词，或点击左侧预设分镜');
      $('prompt').focus();
      return;
    }
    $('composer').requestSubmit();
  };
}

function setSubmitButtonsBusy(submitting) {
  const disabled = submitting || state.uploading > 0;
  if ($('generate')) {
    $('generate').disabled = disabled;
    $('generate').textContent = submitting ? '⏳ 提交中…' : '▶ 加入队列';
  }
  if ($('generateBottomBtn')) {
    $('generateBottomBtn').disabled = disabled;
    const span = $('generateBottomBtn').querySelector('span');
    if (span) span.textContent = submitting ? '正在提交到 Colab 队列…' : '加入生成队列';
  }
  if ($('quickSubmitBtn')) {
    $('quickSubmitBtn').disabled = disabled;
    $('quickSubmitBtn').textContent = submitting ? '⏳ 提交中…' : '▶ 加入生成队列';
  }
}

$('composer').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.uploading) { toast('请等素材上传完成'); return; }
  const promptText = $('prompt').value.trim();
  if (!promptText) {
    toast('请先填写镜头提示词');
    $('prompt').focus();
    return;
  }
  const task = $('task').value;
  const request = {
    task,
    prompt: promptText,
    resolution: $('resolution').value,
    duration: Number($('duration').value),
    aspect: $('aspect').value,
    seed: $('seed').value === '' ? null : Number($('seed').value),
    cpu_offload: $('offload').checked,
    first_image: state.uploads.first_image || '',
    last_image: state.uploads.last_image || '',
    references: task === 'ref2va' ? state.refs.map(({ id, kind }) => ({ id, kind })) : []
  };
  setSubmitButtonsBusy(true);
  try {
    const draftJob=state.jobs.find(j=>j.id===state.inspectorJob&&j.status==='draft');
    const payload = { ...request };
    if (draftJob) payload.draft_created = draftJob.created;
    else if (state.inspectorJob) payload.shot_id = state.inspectorJob;
    const job = await api('/api/jobs', { method: 'POST', body: JSON.stringify(payload) });
    if(draftJob){try{await api(`/api/local/shots/${draftJob.id}/delete`,{method:'POST',body:'{}'});state.jobs=state.jobs.filter(j=>j.id!==draftJob.id);}catch{}}
    state.inspectorJob=job.id;
    state.online = true;
    state.auth = true;
    state.justCreatedJob = job;
    state.jobs = [job, ...state.jobs.filter(j => j.id !== job.id)];
    state.selected = job.id;
    if (state.filter === 'done' || state.filter === 'failed') {
      state.filter = 'all';
      document.querySelectorAll('[data-filter]').forEach(b => b.classList.toggle('active', b.dataset.filter === 'all'));
    }
    tab('create');
    renderCurrent();
    renderHistory();
    toast('✅ 已加入生成队列，单 GPU 依次执行');
    await poll(true);
  } catch (e) {
    toast(e.message);
  } finally {
    setSubmitButtonsBusy(false);
  }
});

function meta(job) {
  const p = job.params || {};
  return `${p.resolution || '480p'} · ${p.duration || 5} 秒 · ${p.aspect || '16:9'} · Seed ${p.seed ?? '--'}`;
}

function localReady(job) { return job.download?.status === 'ready'; }
function transferText(job) {
  const d=job.download||{};
  if(d.status==='ready')return '已保存本机';
  if(d.status==='failed')return '下载失败 · '+(d.error||'请重试');
  if(d.status==='retrying')return '下载中断，正在重试';
  if(d.status==='downloading'){
    const got=(d.downloaded_bytes||0)/1048576,total=(d.total_bytes||0)/1048576;
    return `正在下载到电脑 · ${got.toFixed(2)} / ${total?total.toFixed(2):'--'} MB · ${((d.bytes_per_second||0)/1048576).toFixed(2)} MB/s`;
  }
  return '生成完成，等待自动下载';
}
function statusLabel(job) { return job.status==='done' ? transferText(job) : (labels[job.status]||job.status); }
function jobProgressInfo(job) {
  const p=job.progress||{},d=job.download||{};
  if(job.status==='done'){
    if(localReady(job))return {pct:100,text:'已保存本机 · 100%',determinate:true};
    const pct=d.total_bytes?Math.min(99,Math.round((d.downloaded_bytes||0)/d.total_bytes*100)):0;
    return {pct,text:transferText(job),determinate:!!d.total_bytes,download:true};
  }
  if(job.status==='running'&&p.phase==='sampling'&&p.total>0){
    const pct=Math.round(p.completed/p.total*100);
    return {pct,text:`采样 ${p.completed}/${p.total} · ${pct}%`,determinate:true,completed:p.completed,total:p.total};
  }
  return {pct:0,text:job.stage||labels[job.status],determinate:job.status!=='running'};
}

function progressBar(job) {
  const info=jobProgressInfo(job);
  return `<div class="progress-track ${info.determinate?'':'indeterminate'}"><div class="progress-fill" style="width:${info.determinate?info.pct:35}%"></div></div><div class="progress-caption">${esc(info.text)}</div>`;
}

function selectJob(job, play = false) {
  if (!job) return;
  state.selected = job.id;
  loadInspector(job);
  state.playRequested = play ? job.id : null;
  if (play) {
    state.monitorCollapsed = false;
    if ($('mainVideoPanel')) $('mainVideoPanel').hidden = false;
  }
  renderCurrent();
  renderShotTable();
}

function updateTopQueueTelemetry() {
  const activeJobs = state.jobs.filter(j => ['running', 'queued', 'cancelling'].includes(j.status));
  const runningJob = state.jobs.find(j => j.status === 'running');
  if ($('queueText')) {
    if (runningJob) {
      const info = jobProgressInfo(runningJob);
      $('queueText').textContent = `运行中 (${activeJobs.length}) · ${info.pct}%`;
    } else if (activeJobs.length > 0) {
      $('queueText').textContent = `排队中 (${activeJobs.length})`;
    } else {
      $('queueText').textContent = '队列空闲';
    }
  }
  if ($('queueProgress')) {
    const pct = runningJob ? jobProgressInfo(runningJob).pct : 0;
    $('queueProgress').style.width = `${pct}%`;
  }
}

function renderCurrent() {
  updateTopQueueTelemetry();
  const job = state.jobs.find(j => j.id === state.selected)
    || state.jobs.find(j => ['running', 'cancelling', 'queued'].includes(j.status))
    || state.jobs[0];

  if (!job) {
    delete $('preview').dataset.job;
    $('preview').innerHTML='<div class="empty-preview"><h3>暂无镜头</h3><p>点击新建镜头，填写右侧参数。</p></div>';
    $('current-job').innerHTML = '<div class="queue-empty">队列空闲，在右侧设置镜头参数后即可开始生成。</div>';
    $('result-actions').hidden = true;
    if ($('monitorShotNo')) $('monitorShotNo').textContent = 'IDLE';
    if ($('monitorTitle')) $('monitorTitle').textContent = '等待选择或提交镜头任务';
    if ($('monitorMeta')) $('monitorMeta').textContent = '480p / 768p · 5–15s · 官方原生 6 步采样';
    return;
  }

  state.selected = job.id;
  const idx = state.jobs.length - state.jobs.findIndex(j => j.id === job.id);
  const shotBadge = `#${String(Math.max(1, idx)).padStart(3, '0')}`;
  if ($('monitorShotNo')) $('monitorShotNo').textContent = shotBadge;
  if ($('monitorTitle')) $('monitorTitle').textContent = (job.params && job.params.prompt) ? job.params.prompt : '镜头任务';
  if ($('monitorMeta')) $('monitorMeta').textContent = `${(job.params?.task || 't2va').toUpperCase()} · ${meta(job)}`;

  const terminal = !['queued', 'running', 'cancelling'].includes(job.status);
  const elapsed = Math.max(0, Math.round((job.finished || Date.now() / 1000) - (job.started || job.created)));
  $('current-job').innerHTML = `
    <div class="job-status">
      <span class="badge ${esc(job.status)}">${esc(job.status==='done'?(localReady(job)?'已保存本机':'下载中'):labels[job.status]||job.status)}</span>
      <span class="muted font-mono">${job.status==='draft'?'未生成':elapsed+' 秒'}${job.status === 'queued' ? ' · 等待中' : ''}</span>
    </div>
    <p class="job-stage">${esc(job.status==='done'?'Colab 生成已结束':job.stage||'')}</p>
    ${progressBar(job)}
    <div class="job-meta">${esc(meta(job))}</div>
    ${!terminal ? '<button type="button" class="text-btn" id="cancel-current">✕ 取消此任务</button>' : ''}
  `;
  if ($('cancel-current')) $('cancel-current').onclick = () => cancel(job.id);

  if (localReady(job)) {
    if ($('preview').dataset.job !== job.id) {
      $('preview').dataset.job = job.id;
      $('preview').innerHTML = `<video controls playsinline preload="auto" src="/local-media/${job.id}"></video>`;
    }
    if(state.playRequested===job.id){
      state.playRequested=null;
      $('preview').querySelector('video').play().catch(()=>toast('视频已就绪，点击播放器即可播放'));
    }
    $('result-actions').hidden=false;
    $('result-actions').innerHTML=`<span class="local-ready">✓ 已自动保存到电脑</span><a href="/local-media/${job.id}?download=1" download>另存 MP4</a><button type="button" class="secondary" id="reuse-current">复用参数</button>`;
    $('reuse-current').onclick=()=>reuse(job);
  } else {
    delete $('preview').dataset.job;
    const message=job.status==='done'?transferText(job):(job.stage||'等待 GPU 执行');
    $('preview').innerHTML=`<div class="empty-preview"><div class="frame-art"><span>↓</span></div><h3>${job.status==='done'?'正在保存到电脑':esc(labels[job.status]||'处理中')}</h3><p>${esc(message)}</p></div>`;
    $('result-actions').hidden=false;
    $('result-actions').innerHTML=job.download?.status==='failed'?'<button type="button" id="retry-download" class="secondary">重试下载</button>':'<span>下载完成后可直接本地播放</span>';
    if($('retry-download'))$('retry-download').onclick=()=>retryDownload(job.id);
  }
  if ($('log-details').open || state.tab === 'logs') loadLog();
}

function renderShotTable() {
  const tbody = $('shotRows');
  if (!tbody) return;

  const total = state.jobs.length;
  const activeCount = state.jobs.filter(j => ['queued', 'running', 'cancelling'].includes(j.status)).length;
  const doneCount = state.jobs.filter(localReady).length;
  const failedCount = state.jobs.filter(j => ['failed', 'interrupted', 'cancelled'].includes(j.status)).length;

  if ($('filterCount')) $('filterCount').textContent = `(${total})`;
  if ($('count-all')) $('count-all').textContent = String(total);
  if ($('count-active')) $('count-active').textContent = String(activeCount);
  if ($('count-done')) $('count-done').textContent = String(doneCount);
  if ($('count-failed')) $('count-failed').textContent = String(failedCount);
  if ($('sceneSummary')) {
    $('sceneSummary').textContent = `${total} 个 Shot · ${activeCount} 个运行中 · ${doneCount} 个已完成`;
  }

  const filtered = state.jobs.filter(j => {
    if (state.filter === 'active') return ['queued', 'running', 'cancelling'].includes(j.status)||(j.status==='done'&&!localReady(j));
    if (state.filter === 'done') return localReady(j);
    if (state.filter === 'failed') return ['failed', 'interrupted', 'cancelled'].includes(j.status);
    return true;
  });

  if (!filtered.length) {
    tbody.innerHTML = `<tr class="table-empty-row"><td colspan="6">当前筛选下暂无镜头任务。在右侧配置提示词与参数后点击「加入生成队列」。</td></tr>`;
    return;
  }

  // 队列按时间正序：老的在上面，新建的追加在最下面。
  const ordered = filtered.slice().reverse();
  tbody.innerHTML = ordered.map(job => {
    const idx = state.jobs.length - state.jobs.findIndex(j => j.id === job.id);
    const shotNo = `#${String(Math.max(1, idx)).padStart(3, '0')}`;
    const taskMode = (job.params?.task || 't2va').toLowerCase();
    const info = jobProgressInfo(job);
    const elapsed = Math.max(0, Math.round((job.finished || Date.now() / 1000) - (job.started || job.created)));
    const isRunning = ['queued', 'running', 'cancelling'].includes(job.status);
    const isSelected = state.selected === job.id;

    return `
      <tr class="${isSelected ? 'active' : ''}" data-row-job="${esc(job.id)}">
        <td class="font-mono" style="font-weight:700">${esc(shotNo)}</td>
        <td><span class="mode-badge ${esc(taskMode)}">${esc(taskMode.toUpperCase())}</span></td>
        <td>
          <div class="shot-row-prompt">${esc(job.params?.prompt || '未命名镜头')}</div>
          <div class="shot-row-sub">${esc(meta(job))}</div>
        </td>
        <td>
          <div style="display:flex;align-items:center;gap:8px">
            <div class="mini-bar-track" style="flex:1"><div class="mini-bar-fill" style="width:${info.pct}%"></div></div>
            <span class="font-mono" style="font-size:11px;color:var(--text-muted)">${info.pct}%</span>
          </div>
          <div style="font-size:10.5px;color:var(--text-secondary);margin-top:3px">${esc(info.text)}</div>
        </td>
        <td class="font-mono">${job.status==='draft'?'—':elapsed+'s'}</td>
        <td style="text-align:right">
          <div style="display:inline-flex;gap:4px;flex-wrap:nowrap">
            ${localReady(job) ? `<button type="button" class="btn btn-ghost btn-xs" data-row-action="play" data-job="${esc(job.id)}">▶ 播放</button>` : ''}
            <button type="button" class="btn btn-ghost btn-xs" data-row-action="reuse" data-job="${esc(job.id)}">📋 复用</button>
            ${localReady(job) ? `<a class="btn btn-ghost btn-xs" href="/local-media/${esc(job.id)}?download=1" download>⬇</a>` : ''}
            ${isRunning ? `<button type="button" class="btn btn-ghost btn-xs" style="color:#fb7185" data-row-action="cancel" data-job="${esc(job.id)}">✕ 取消</button>` : `<button type="button" class="btn btn-ghost btn-xs" data-row-action="delete" data-job="${esc(job.id)}">删除</button>`}
          </div>
        </td>
      </tr>
    `;
  }).join('');

  tbody.querySelectorAll('[data-row-job]').forEach(tr => {
    tr.onclick = () => {
      const job = state.jobs.find(j => j.id === tr.dataset.rowJob);
      if (job) selectJob(job);
    };
  });

  tbody.querySelectorAll('[data-row-action]').forEach(btn => {
    btn.onclick = event => {
      event.stopPropagation();
      const job = state.jobs.find(j => j.id === btn.dataset.job);
      if (!job) return;
      const act = btn.dataset.rowAction;
      if (act === 'play') selectJob(job, true);
      else if (act === 'reuse') reuse(job);
      else if (act === 'cancel') cancel(job.id);
      else if (act === 'delete') deleteShot(job);
    };
  });
}

document.querySelectorAll('[data-filter]').forEach(btn => {
  btn.onclick = () => {
    state.filter = btn.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(b => b.classList.toggle('active', b.dataset.filter === state.filter));
    renderShotTable();
  };
});

if ($('toggleMonitorBtn')) {
  $('toggleMonitorBtn').onclick = () => {
    state.monitorCollapsed = !state.monitorCollapsed;
    $('mainVideoPanel').hidden = state.monitorCollapsed;
  };
}
if ($('closeMonitorBtn')) {
  $('closeMonitorBtn').onclick = () => {
    state.monitorCollapsed = true;
    $('mainVideoPanel').hidden = true;
  };
}
if ($('openLogsBtn')) {
  $('openLogsBtn').onclick = () => tab('logs');
}
if ($('refreshFullLogBtn')) {
  $('refreshFullLogBtn').onclick = loadLog;
}

async function cancel(id) {
  try {
    await api(`/api/jobs/${id}/cancel`, { method: 'POST', body: '{}' });
    await poll();
  } catch (e) {
    toast(e.message);
  }
}

async function loadLog() {
  if (!state.selected || !state.auth) return;
  try {
    const data = await api(`/api/jobs/${state.selected}/log`);
    $('logs').textContent = data.log;
    if ($('full-logs')) $('full-logs').textContent = data.log;
    if ($('logJobIndicator')) $('logJobIndicator').textContent = `任务 ${state.selected.slice(0, 8)} 日志`;
  } catch (e) {
    $('logs').textContent = e.message;
    if ($('full-logs')) $('full-logs').textContent = e.message;
  }
}
$('log-details').addEventListener('toggle', () => { if ($('log-details').open) loadLog(); });

async function retryDownload(id){
  try{await api(`/api/local/jobs/${id}/retry`,{method:'POST',body:'{}'});toast('已重新加入下载队列');poll(true);}catch(e){toast(e.message);}
}
function renderHistory() {
  const items=state.jobs.filter(localReady);
  $('job-count').textContent=items.length;
  renderShotTable();
  $('history-grid').innerHTML=items.length?items.map(job=>`<article class="gallery-card"><div class="gallery-info"><span class="badge done">✓ 已保存本机</span><p class="prompt-excerpt">${esc(job.params?.prompt||'视频')}</p><div class="job-meta">${esc(meta(job))}</div><div class="card-actions"><button type="button" data-local-play="${job.id}">▶ 本地播放</button><a href="/local-media/${job.id}?download=1" download>另存 MP4</a><button type="button" data-local-reuse="${job.id}">复用参数</button></div></div></article>`).join(''):'<div class="gallery-empty">生成完成后会自动下载到电脑，下载好的视频在这里。</div>';
  $('history-grid').querySelectorAll('[data-local-play]').forEach(b=>b.onclick=()=>{tab('create');selectJob(state.jobs.find(j=>j.id===b.dataset.localPlay),true);});
  $('history-grid').querySelectorAll('[data-local-reuse]').forEach(b=>b.onclick=()=>reuse(state.jobs.find(j=>j.id===b.dataset.localReuse)));
}

async function reuse(job) {
  try{
    const copy=await api('/api/local/shots',{method:'POST',body:JSON.stringify(job.editor_params||job.params)});
    state.jobs=[copy,...state.jobs];tab('create');selectJob(copy);toast('已复制成新镜头草稿');
  }catch(e){toast(e.message);}
}

let historySignature = '';
async function poll(force = false) {
  if (state.polling) {
    if (force) state.pollQueued = true;
    return;
  }
  state.polling = true;
  try {
    const data = await api('/api/local/state');
    state.online=!!data.engine_online;
    state.auth=state.online;
    setConnection(state.online?'Colab A100 在线':'Colab 离线 · 可播放本地作品',state.online?'online':'offline');
    if($('output-directory'))$('output-directory').textContent='自动保存位置：'+data.output_directory;
    let jobs = Array.isArray(data.jobs) ? data.jobs : [];
    if (state.justCreatedJob) {
      if (jobs.some(j => j.id === state.justCreatedJob.id)) {
        state.justCreatedJob = null;
      } else {
        jobs = [state.justCreatedJob, ...jobs];
      }
    }
    state.jobs = jobs;
    if(!state.inspectorJob&&jobs.length)loadInspector(jobs.find(j=>j.id===state.selected)||jobs[0]);
    renderCurrent();
    const signature = JSON.stringify(jobs);
    if (signature !== historySignature) {
      historySignature = signature;
      renderHistory();
    } else {
      renderShotTable();
    }
  } catch (e) {
    state.online = false;
    setConnection('Colab 未连接', 'offline');
  } finally {
    state.polling = false;
    if (state.pollQueued) {
      state.pollQueued = false;
      poll();
    }
  }
}

$('refresh').onclick = () => poll(true);
$('settings').onclick = () => {
  try { $('colab-url').value = localStorage.getItem('pika-colab-url') || ''; } catch {}
  $('settings-dialog').showModal();
};
$('save-settings').onclick = () => {
  try {
    const raw = $('colab-url').value.trim();
    const u = new URL(raw);
    if (u.protocol !== 'https:' || u.hostname !== 'colab.research.google.com') throw new Error();
    localStorage.setItem('pika-colab-url', u.href);
    toast('Colab 链接已保存');
  } catch {
    toast('请填写有效的 Colab HTTPS 笔记本链接');
  }
};
$('open-colab').onclick = () => {
  try {
    const u = new URL($('colab-url').value);
    if (u.protocol !== 'https:' || u.hostname !== 'colab.research.google.com') throw new Error();
    window.open(u.href, '_blank', 'noopener');
  } catch {
    toast('请先填写有效的 Colab 链接');
  }
};

async function boot() {
  await poll();
  setInterval(poll, 3500);
}
boot();
