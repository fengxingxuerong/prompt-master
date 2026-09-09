"""
PromptMaster Web 控制台（单 HTML 页面，内联 CSS/JS，无外部依赖）。

功能：
- 提交单个优化任务（task / target_model / 用例数 / 迭代上限）
- 任务列表：轮询状态（实时聚合分 / 迭代 / LLM 调用数）
- 查看交付报告（Markdown 渲染为 HTML）
- 批量赛马：一次提交多个任务，看横向对比报告

由 server.py 的 GET / 路由提供。
"""

WEB_CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PromptMaster 控制台</title>
<style>
  :root {
    --bg: #0f1117; --panel: #171a23; --border: #262b3a;
    --text: #e6e9f0; --muted: #8b93a7; --accent: #5b8cff; --ok: #3ecf8e; --warn: #ffb454;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: "Segoe UI", "Microsoft YaHei", sans-serif; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: 360px 1fr; gap: 20px; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .panel h2 { font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 12px; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 4px; }
  input, textarea { width: 100%; background: #0d0f15; border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 8px 10px; font-size: 13px; font-family: inherit; }
  textarea { min-height: 72px; resize: vertical; }
  button { background: var(--accent); color: #fff; border: 0; border-radius: 6px; padding: 9px 16px;
    font-size: 13px; cursor: pointer; margin-top: 12px; }
  button.secondary { background: #2a3040; }
  button:hover { filter: brightness(1.15); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .row { display: flex; gap: 8px; }
  .row > div { flex: 1; }
  #tasks { display: flex; flex-direction: column; gap: 10px; max-height: 70vh; overflow-y: auto; }
  .task { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; cursor: pointer; }
  .task:hover { border-color: var(--accent); }
  .task .top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .task .rid { font-family: monospace; font-size: 12px; color: var(--muted); }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 20px; background: #2a3040; }
  .badge.running { background: #25314d; color: #8ab4ff; }
  .badge.passed { background: #123524; color: var(--ok); }
  .badge.failed { background: #3d1f24; color: #ff8080; }
  /* max_iterations / early_stopped 都是“未达标交付”，给单独配色，否则只能看到默认灰徒章 */
  .badge.max_iterations { background: #3d1f24; color: #ff8080; }
  .badge.early_stopped { background: #3a2e17; color: var(--warn); }
  .task .meta { font-size: 12px; color: var(--muted); display: flex; gap: 14px; flex-wrap: wrap; }
  #report { white-space: pre-wrap; font-size: 13px; line-height: 1.7; max-height: 70vh; overflow-y: auto; }
  #report h1 { font-size: 18px; margin: 12px 0 8px; }
  #report code { background: #0d0f15; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
  .empty { color: var(--muted); font-size: 13px; text-align: center; padding: 40px 0; }
  .error { color: #ff8080; font-size: 12px; margin-top: 8px; }
</style>
</head>
<body>
  <h1>PromptMaster 控制台</h1>
  <div class="sub">提示词自动生成 / 测试 / 评估 / 迭代优化 · 异步任务 · 批量赛马</div>

  <div class="grid">
    <div>
      <div class="panel">
        <h2>提交任务</h2>
        <label>原始需求</label>
        <textarea id="task" placeholder="例如：帮我写一个 prompt，让 AI 分析销售数据并给出可落地的增长建议"></textarea>
        <label>目标模型</label>
        <input id="targetModel" value="DeepSeek-V4-Flash" placeholder="提示词最终运行的目标模型">
        <div class="row">
          <div><label>测试用例数</label><input id="cases" type="number" value="2" min="1" max="8"></div>
          <div><label>最大迭代</label><input id="maxIter" type="number" value="2" min="1" max="10"></div>
        </div>
        <button id="submitBtn">提交优化任务</button>
        <div class="row" style="margin-top:8px">
          <button id="submitBtn2" class="secondary" style="flex:1">+ 加入赛马队列</button>
          <button id="raceBtn" class="secondary" style="flex:1">🚀 发起赛马</button>
        </div>
        <div id="submitError" class="error"></div>
      </div>
      <div class="panel" style="margin-top:20px">
        <h2>任务列表（点击查看报告）</h2>
        <div id="tasks"><div class="empty">暂无任务，先提交一个吧</div></div>
      </div>
    </div>

    <div class="panel">
      <h2 id="reportTitle">报告 / 状态</h2>
      <div id="report" class="empty">点击左侧任务查看交付报告</div>
    </div>
  </div>

<script>
const $ = id => document.getElementById(id);
const tasks = {};   // run_id -> {rid, status, iter, avg, min, viewed}
const races = {};   // race_id -> {total, finished, name}
let raceQueue = []; // 待赛马的任务
let polling = false; // 全局只跑一条轮询链（旧写法每提交一次就多一条，量随提交数线性增长）

// rid / status 都来自服务端，仍统一转义：免得日后改成用户可控 ID 时直接变成 XSS 注入点
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, method = 'GET', body) {
  const headers = { 'Content-Type': 'application/json' };
  const r = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const d = (await r.json()).detail;
      // 422 的 detail 是数组，直接拼会变成 "[object Object]"
      detail = typeof d === 'string' ? d : JSON.stringify(d);
    } catch { /* 非 JSON 响应体 */ }
    throw new Error(detail || r.statusText);
  }
  return r.json();
}

function ensurePolling() {
  if (polling) return;
  polling = true;
  poll();
}

function hasActive() {
  return Object.values(tasks).some(t => t.status === 'running' || t.status === 'pending')
    || Object.keys(races).length > 0;
}

function submitTask() {
  const task = $('task').value.trim();
  if (!task) { $('submitError').textContent = '请填写需求描述'; return; }
  $('submitBtn').disabled = true;
  $('submitError').textContent = '';
  api('/api/optimize', 'POST', {
    task,
    target_model: $('targetModel').value.trim() || '未指定',
    n_test_cases: parseInt($('cases').value) || 2,
    max_iterations: parseInt($('maxIter').value) || 2,
  }).then(d => {
    tasks[d.run_id] = { rid: d.run_id, status: 'running' };
    renderTasks(); ensurePolling();
    $('submitBtn').disabled = false;
  }).catch(e => { $('submitError').textContent = e.message; $('submitBtn').disabled = false; });
}

function enqueueRace() {
  const task = $('task').value.trim();
  if (!task) { $('submitError').textContent = '请填写需求描述'; return; }
  raceQueue.push({
    task,
    target_model: $('targetModel').value.trim() || '未指定',
    n_test_cases: parseInt($('cases').value) || 2,
    max_iterations: parseInt($('maxIter').value) || 2,
  });
  $('submitError').textContent = `✅ 已入赛马队列（${raceQueue.length} 个）`;
}

function launchRace() {
  if (raceQueue.length < 2) { $('submitError').textContent = '赛马至少需要 2 个任务，先用「加入赛马队列」攒够'; return; }
  if (raceQueue.length > 10) { $('submitError').textContent = '赛马最多 10 个任务，当前已攒 ' + raceQueue.length + ' 个'; return; }
  api('/api/race', 'POST', { name: '控制台赛马', tasks: raceQueue })
    .then(d => {
      raceQueue = [];
      races[d.race_id] = { total: 0, finished: 0, name: '控制台赛马' };
      $('submitError').textContent = `🚀 赛马已启动：${d.race_id}（进度会显示在这里）`;
      ensurePolling();
    })
    .catch(e => { $('submitError').textContent = '赛马启动失败：' + e.message; }); // 旧写法没 catch，422 静默失败
}

function renderTasks() {
  const box = $('tasks');
  const keys = Object.keys(tasks);
  if (!keys.length) { box.innerHTML = '<div class="empty">暂无任务，先提交一个吧</div>'; return; }
  box.innerHTML = keys.map(rid => {
    const t = tasks[rid];
    const cls = (t.status === 'running' || t.status === 'pending') ? 'running' : t.status;
    return `<div class="task" onclick="viewReport('${esc(rid)}')">
      <div class="top"><span class="rid">${esc(rid)}</span><span class="badge ${cls}">${esc(t.status)}</span></div>
      <div class="meta">
        <span>迭代 ${t.iter ?? '-'}</span><span>平均 ${t.avg ?? '-'}</span><span>最低 ${t.min ?? '-'}</span>
      </div></div>`;
  }).join('');
}

async function poll() {
  for (const rid of Object.keys(tasks)) {
    const t = tasks[rid];
    if (t.status === 'running' || t.status === 'pending') {
      try {
        const s = await api(`/api/status/${rid}`);
        const wasActive = t.status === 'running' || t.status === 'pending';
        t.status = s.status; t.iter = s.iteration;
        t.avg = s.aggregate?.avg_score; t.min = s.aggregate?.min_score;
        // 只在“刚刚转终态”时自动拉一次报告，之后不再抓占用户正在看的面板
        if (wasActive && !['running', 'pending'].includes(s.status) && !t.viewed) {
          t.viewed = true;
          viewReport(rid);
        }
      } catch (e) { /* 服务重启等暂忽略 */ }
    }
  }
  for (const rid of Object.keys(races)) {
    try {
      const r = await api(`/api/race/${rid}`);
      Object.assign(races[rid], r);
      $('submitError').textContent = `🏇 赛马 ${rid}：${r.finished}/${r.total} 完成`
        + (r.finished >= r.total ? ` — 对比报告：GET /api/race/${rid}/report` : '');
      if (r.total && r.finished >= r.total) delete races[rid];
    } catch (e) { delete races[rid]; }
  }
  renderTasks();
  if (hasActive()) setTimeout(poll, 5000);
  else polling = false; // 没在跑的任务就停下来，不要 5s 一次空转
}

async function viewReport(rid) {
  $('reportTitle').textContent = `报告 / ${rid}`;
  try {
    const d = await api(`/api/report/${rid}`);
    $('report').textContent = d.report;
  } catch (e) {
    $('report').textContent = '（报告未生成，任务可能仍在运行）';
  }
}

$('submitBtn').onclick = submitTask;
$('submitBtn2').onclick = enqueueRace;
$('raceBtn').onclick = launchRace;
ensurePolling();
</script>
</body>
</html>
"""
