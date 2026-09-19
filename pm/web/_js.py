"""Web 控制台脚本（从原 pm/web.py 拆出，按功能域分 const，拼接后与拆分前逐字节一致）。

分段说明（拼接顺序即原文件出现顺序，函数提升保证运行顺序不变）：
- JS_CORE      基础设施：$ / esc / 状态 / API / 健康检查 / 提交 / 轮询 / 左栏渲染 / 选中
- JS_CHAT      对话标签渲染
- JS_PIPELINE  流水线标签渲染
- JS_PROMPT    提示词标签渲染（版本 diff）
- JS_SCORE     评分标签渲染
- JS_CANVAS    canvas 雷达图 + 版本曲线
- JS_MD        Markdown 轻量渲染（XSS 防线核心）
- JS_TOOLS     复制工具
- JS_RACE      批量赛马
- JS_EVENTS    事件委托（CSP 兼容）
- JS_HISTORY   历史面板
- JS_INIT      初始化钩子

注意：这些是 **raw 字符串**（r\"\"\"...\"\"\"）——JS 源码里的正则转义
（\\n、\\w、\\s、\\* 等）必须原样保留，普通字符串会把它们吃掉。
"""

JS_CORE = r""""use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ---- 状态 ----
const runs = {};          // rid -> {status, iteration, aggregate, task, trace, evaluations, prompt_version_list, ...}
const raceQueue = [];
let activeRid = null;
let polling = false;
let curTab = "chat";
const copyStore = {};     // 键 -> 待复制文本（避免把长文本塞进 onclick 属性触发转义陷阱）

// ---- API ----
async function api(path, method="GET", body){
  const r = await fetch(path, {method, headers:{"Content-Type":"application/json"}, body: body ? JSON.stringify(body) : undefined});
  if(!r.ok){
    let d = r.statusText;
    try{ const j = await r.json(); d = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); }catch(_){}
    throw new Error(d || r.statusText);
  }
  return r.json();
}

// ---- 健康检查 ----
async function checkHealth(){
  try{ const h = await api("/api/health"); $("health").className = "health ok"; $("healthText").textContent = "服务正常"; }
  catch(_){ $("health").className = "health"; $("healthText").textContent = "服务离线"; }
}
checkHealth(); setInterval(checkHealth, 30000);

// ---- 提交任务 ----
async function submitTask(task){
  if(!task || !task.trim()) return;
  $("sendBtn").disabled = true; $("ciErr").textContent = "";
  let rid;
  try{
    const d = await api("/api/optimize", "POST", {
      task: task.trim(),
      target_model: $("targetModel").value.trim() || "未指定",
      n_test_cases: parseInt($("optCases").value) || 2,
      max_iterations: parseInt($("optIter").value) || 2,
    });
    rid = d.run_id;
    runs[rid] = {status:"running", iteration:0, task:task.trim(), trace:[], evaluations:[], prompt_version_list:[],
                 aggregate:null, baseline_aggregate:null, pairwise:null, target_model:$("targetModel").value.trim()};
    renderRunList();
    selectRun(rid);
    $("chatInput").value = "";
    ensurePolling();
  }catch(e){
    $("ciErr").textContent = e.message;
  }finally{
    $("sendBtn").disabled = false;
  }
}

// ---- 轮询 ----
function ensurePolling(){ if(!polling){ polling = true; poll(); } }
function hasActive(){ return Object.values(runs).some(r => r.status==="running" || r.status==="pending"); }

async function poll(){
  for(const rid of Object.keys(runs)){
    const r = runs[rid];
    if(r.status === "running" || r.status === "pending"){
      try{
        const s = await api(`/api/status/${rid}`);
        Object.assign(r, s);
        if(activeRid === rid) renderActive();
        renderRunList();
      }catch(_){}
    }
  }
  if(hasActive()) setTimeout(poll, 3000);
  else polling = false;
}

// ---- 左栏渲染 ----
function renderRunList(){
  const box = $("runList");
  const keys = Object.keys(runs);
  if(!keys.length){ box.innerHTML = '<div class="u-empty-side">还没有任务，在下方输入需求开始</div>'; return; }
  // 最新提交在最上
  box.innerHTML = keys.reverse().map(rid => {
    const r = runs[rid];
    const agg = r.aggregate || {};
    const cls = (r.status==="running"||r.status==="pending") ? "running" : r.status;
    return `<div class="run-item ${rid===activeRid?'active':''}" data-run="${esc(rid)}">
      <div class="ri-top"><span class="ri-id">${esc(rid.slice(0,8))}</span><span class="badge ${cls}">${esc(r.status)}</span></div>
      <div class="ri-task">${esc(r.task ? r.task.slice(0,40) : (rid.slice(0,8)))}</div>
      <div class="ri-meta"><span>迭代 ${r.iteration ?? 0}</span><span>均分 ${agg.avg_score ?? '-'}</span><span>${r.llm_calls ?? 0} 调用</span></div>
    </div>`;
  }).join("");
}

// ---- 选中任务 ----
function selectRun(rid){
  activeRid = rid;
  renderRunList();
  renderActive();
}

// ---- 渲染当前任务（全部标签）----
function renderActive(){
  const r = runs[activeRid];
  if(!r) return;
  $("cntTrace").textContent = (r.trace||[]).length;
  $("cntPV").textContent = (r.prompt_version_list||[]).length;
  renderChat(r);
  renderPipeline(r);
  renderPromptView(r);
  renderScoreView(r);
}
"""

JS_CHAT = r"""// ========== 对话标签 ==========
function renderChat(r){
  const box = $("chatFlow");
  if(!r){ box.innerHTML = '<div class="empty-ws">在底部输入你的提示词需求</div>'; return; }
  const parts = [];
  // 用户气泡：原始需求
  parts.push(msgUser("你", r.task || "(空需求)"));

  // agent 实时状态卡片
  const agg = r.aggregate || {};
  const running = r.status === "running" || r.status === "pending";
  let card = '<div class="bubble-card">';
  card += `<div class="bc-row"><span>状态</span><b><span class="badge ${r.status}">${esc(r.status)}</span></b></div>`;
  card += `<div class="bc-row"><span>迭代</span><b>${r.iteration ?? 0}</b></div>`;
  card += `<div class="bc-row"><span>LLM 调用</span><b>${r.llm_calls ?? 0}</b></div>`;
  if(agg.avg_score != null) card += `<div class="bc-row"><span>当前均分 / 最低</span><b>${agg.avg_score} / ${agg.min_score}</b></div>`;
  if(r.early_stop_reason) card += `<div class="bc-row"><span>早停原因</span><b class="u-warn">${esc(r.early_stop_reason)}</b></div>`;
  if((r.prompt_quality_issues||[]).length) card += `<div class="bc-row"><span>质量警告</span><b class="u-warn">${r.prompt_quality_issues.length} 条</b></div>`;
  card += "</div>";

  if(running){
    parts.push(msgBot("Agent", `<div class="progress-line"><span class="spinner"></span>正在 ${curNodeLabel(r)}…</div>` + card));
  } else {
    parts.push(msgBot("Agent", card));
  }

  // 最终提示词（取最高分版本）
  const pvs = r.prompt_version_list || [];
  if(pvs.length){
    const best = [...pvs].sort((a,b)=>(b.avg_score||0)-(a.avg_score||0))[0];
    copyStore.best = best.prompt || "";
    const note = best.note ? ` · ${esc(best.note)}` : "";
    parts.push(msgBot("交付提示词", `<button class="copybtn" data-copy="best">复制</button><b>第 ${best.iteration ?? 0} 版${note} · 均分 ${best.avg_score ?? '-'}</b><div class="prompt-box">${esc(best.prompt)}</div>`));
  }

  // 报告
  if(r.status && r.status !== "running" && r.status !== "pending"){
    parts.push(`<div class="msg bot"><div class="ava">📋</div><div class="body"><div class="who">交付报告</div><div class="content"><button class="copybtn" id="rptBtn" data-report="${esc(activeRid)}">加载完整报告</button><div id="rptBox" class="u-mt8"></div></div></div></div>`);
  }

  box.innerHTML = parts.join("");
}

function msgUser(who, text){ return `<div class="msg user"><div class="ava">你</div><div class="body"><div class="who">${esc(who)}</div><div class="content">${esc(text)}</div></div></div>`; }
function msgBot(who, html){ return `<div class="msg bot"><div class="ava">✦</div><div class="body"><div class="who">${esc(who)}</div><div class="content">${html}</div></div></div>`; }

function curNodeLabel(r){
  const t = (r.trace||[]).filter(x=>x.node).slice(-1)[0];
  return t ? `${t.node} · ${t.event||""}` : "初始化";
}

async function loadReport(rid){
  $("rptBtn").textContent = "加载中…"; $("rptBtn").disabled = true;
  try{
    const d = await api(`/api/report/${rid}`);
    $("rptBox").innerHTML = `<div class="md">${md2html(d.report)}</div>`;
    $("rptBtn").style.display = "none";
  }catch(e){
    $("rptBox").innerHTML = `<span class="u-bad">${esc(e.message)}</span>`;
    $("rptBtn").textContent = "重试"; $("rptBtn").disabled = false;
  }
}
"""

JS_PIPELINE = r"""// ========== 流水线标签 ==========
const PIPE_NODES = ["clarify","ask_user","optimize","mock","baseline","test","evaluate","revise","compare","report"];
const NODE_ICONS = {clarify:"🔍",ask_user:"❓",optimize:"✨",mock:"🎲",baseline:"📐",test:"🧪",evaluate:"⚖️",revise:"🔧",compare:"🏁",report:"📦"};
function renderPipeline(r){
  const box = $("pipeline");
  const trace = r.trace || [];
  if(!trace.length){ box.innerHTML = '<div class="empty-ws">提交任务后，节点级执行轨迹会出现在这里</div>'; return; }
  // 按迭代分组
  const groups = {};
  trace.forEach(t => { const it = t.iteration ?? 0; (groups[it] = groups[it]||[]).push(t); });
  const iters = Object.keys(groups).sort((a,b)=>+a-+b);
  let html = "";
  iters.forEach(it => {
    html += `<div class="iter-group"><div class="ig-head"><span class="pill">iter ${it}</span><span>${groups[it].length} 步</span></div>`;
    groups[it].forEach(t => {
      const node = t.node || "?";
      const ev = t.event || "";
      const isRun = ev && ev.includes("done")===false && ev.includes("skip")===false && ev.includes("fail")===false;
      const isSkip = ev.includes("skip");
      const isFail = ev.includes("fail");
      const cls = isFail ? "fail" : isSkip ? "skip" : "done";
      const ic = NODE_ICONS[node] || "•";
      let meta = "";
      if(t.avg != null) meta += `<span>均分 ${t.avg}</span>`;
      if(t.min != null) meta += `<span>最低 ${t.min}</span>`;
      if(t.n_cases != null) meta += `<span>用例 ${t.n_cases}</span>`;
      if(t.n_samples != null) meta += `<span>采样 ${t.n_samples}</span>`;
      if(t.concurrency != null) meta += `<span>并发 ${t.concurrency}</span>`;
      if(t.calls != null) meta += `<span>${t.calls} 调用</span>`;
      if(t.reason) meta += `<span>${esc(t.reason)}</span>`;
      if(t.error) meta += `<span class="u-bad">${esc(t.error)}</span>`;
      html += `<div class="node-row"><div class="node-ic ${cls}">${ic}</div><div class="node-info"><div class="ni-name">${esc(node)}</div><div class="ni-event">${esc(ev)}</div><div class="ni-meta">${meta}</div></div></div>`;
    });
    html += "</div>";
  });
  box.innerHTML = html;
}
"""

JS_PROMPT = r"""// ========== 提示词标签 ==========
let pvA = 0, pvB = 0;
function renderPromptView(r){
  const box = $("promptView");
  const pvs = r.prompt_version_list || [];
  if(!pvs.length){ box.innerHTML = '<div class="empty-ws">生成的提示词版本与 diff 会出现在这里</div>'; return; }
  if(pvA >= pvs.length) pvA = pvs.length - 1;
  if(pvB >= pvs.length) pvB = pvs.length - 1;
  const opts = pvs.map((v,i)=>`<option value="${i}" ${i===pvA?'selected':''}>v${i} · iter${v.iteration ?? i} · 均${v.avg_score ?? '-'}</option>`).join("");
  const optsB = pvs.map((v,i)=>`<option value="${i}" ${i===pvB?'selected':''}>v${i} · iter${v.iteration ?? i} · 均${v.avg_score ?? '-'}</option>`).join("");
  const cur = pvs[pvA] || {};
  copyStore.pv = cur.prompt || "";
  box.innerHTML = `
    <div class="pv-bar">
      <select id="pvSelectA" data-pv="A">${opts}</select>
      <span class="u-muted">↔ 对比</span>
      <select id="pvSelectB" data-pv="B">${optsB}</select>
      <button class="copybtn" data-copy="pv">复制当前版</button>
    </div>
    <div class="pv-cols">
      <div class="pv-col"><h4>v${pvA} 全文（${(cur.prompt||"").length} 字）</h4><div class="prompt-box u-nomax">${esc(cur.prompt||"")}</div></div>
      <div class="pv-col"><h4>diff v${pvA} ↔ v${pvB}</h4>${renderDiff(pvs[pvA]?.prompt||"", pvs[pvB]?.prompt||"")}</div>
    </div>`;
}
function renderDiff(a, b){
  const la = (a||"").split("\n"), lb = (b||"").split("\n");
  const n = Math.max(la.length, lb.length);
  let rows = "";
  for(let i=0;i<n;i++){
    const x = la[i] ?? "", y = lb[i] ?? "";
    const same = x === y;
    rows += `<div class="dc ${same?'eq':(x?'del':'')}">${same?esc(x):esc(x||"")}</div><div class="dc ${same?'eq':(y?'add':'')}">${same?esc(y):esc(y||"")}</div>`;
  }
  return `<div class="diff">${rows}</div>`;
}
"""

JS_SCORE = r"""// ========== 评分标签 ==========
function renderScoreView(r){
  const box = $("scoreView");
  const agg = r.aggregate || {};
  const base = r.baseline_aggregate || {};
  const pw = r.pairwise || {};
  const evals = r.evaluations || [];
  if(!r.status || r.status==="pending"){ box.innerHTML = '<div class="empty-ws">评分雷达图、版本曲线、基线 Δ 与盲评结论会出现在这里</div>'; return; }

  const dims = ["task_completion","format_adherence","constraint_compliance","robustness","quality"];
  const dimCN = {task_completion:"任务完成度",format_adherence:"格式遵从",constraint_compliance:"约束遵守",robustness:"鲁棒性",quality:"质量与深度"};
  // 取最新评估的维度分（用于雷达图）
  const latest = evals.length ? evals[evals.length-1] : null;
  const ds = latest && latest.dimension_scores ? latest.dimension_scores : {};

  const passed = agg.passed;
  const delta = (agg.avg_score != null && base.avg_score != null) ? (agg.avg_score - base.avg_score) : null;

  let cards = `<div class="score-cards">
    <div class="stat-card"><h4>判定</h4><div class="big ${passed?'ok':'bad'}">${passed?'通过':'未达标'}</div><div class="sub">阈值 ${agg.avg_score!=null?'≥ 8.0':''}</div></div>
    <div class="stat-card"><h4>聚合分数</h4><div class="big">${agg.avg_score ?? '-'}</div><div class="sub">最低 ${agg.min_score ?? '-'} · 下界 ${agg.ci_lower ?? '-'}</div></div>`;
  if(delta != null){
    const dCls = delta > 0 ? "ok" : (delta < 0 ? "bad" : "");
    cards += `<div class="stat-card"><h4>相对基线 Δ</h4><div class="big ${dCls}">${delta>0?'+':''}${delta.toFixed(2)}</div><div class="sub">基线均分 ${base.avg_score ?? '-'}</div></div>`;
  }
  if(pw && pw.verdict){
    const vColor = pw.verdict==="better"?"ok":(pw.verdict==="worse"?"bad":"");
    cards += `<div class="stat-card"><h4>成对盲评</h4><div class="big ${vColor}">${esc(pw.verdict)}</div><div class="sub">胜${(pw.votes&&pw.votes.better)||0}/负${(pw.votes&&pw.votes.worse)||0}/平${(pw.votes&&pw.votes.tie)||0}${pw.conflict?' · ⚠️结论冲突':''}</div></div>`;
  }
  if(agg.noise != null) cards += `<div class="stat-card"><h4>采样噪声</h4><div class="big">${agg.noise.toFixed(2)}</div><div class="sub">SEM ${agg.sem ?? '-'} · 样本 ${agg.n_samples ?? '-'}</div></div>`;
  if(agg.judge_bias != null) cards += `<div class="stat-card"><h4>评委偏差</h4><div class="big ${agg.judge_bias_warning?'u-warn':'u-text'}">${agg.judge_bias.toFixed(2)}</div><div class="sub">${agg.judge_bias_warning?'⚠️ 可能放水':'正常'}</div></div>`;
  cards += "</div>";

  // 评估表
  let evalTable = "";
  if(evals.length){
    evalTable = `<div class="chart-box u-mt14"><h4>逐用例评估</h4><table class="md u-w100"><tr><th>用例</th><th>加权分</th><th>自报分</th><th>评委</th><th>通过</th><th>问题</th></tr>`;
    evals.forEach(e => {
      const issues = (e.issues||[]).length;
      evalTable += `<tr><td>#${e.test_case_index ?? '-'}</td><td><b>${e.weighted_score ?? '-'}</b></td><td>${e.model_reported_score ?? '-'}</td><td>${esc(e.judge||'-')}</td><td>${e.passed?'✓':'✗'}</td><td>${issues?`${issues} 条`:''}</td></tr>`;
    });
    evalTable += "</table></div>";
  }

  box.innerHTML = `
    <div class="score-grid">
      <div>
        <div class="chart-box"><h4>维度雷达图（最新评估）</h4><canvas id="radar" width="320" height="320"></canvas></div>
        <div class="chart-box u-mt14"><h4>版本分数曲线</h4><canvas id="curve" width="320" height="200"></canvas></div>
      </div>
      ${cards}${evalTable}
    </div>`;

  // 画图
  drawRadar("radar", dims.map(d=>ds[d] ?? 0), dims.map(d=>dimCN[d]));
  drawCurve("curve", (r.prompt_version_list||[]).map(v=>v.avg_score));
}
"""

JS_CANVAS = r"""// ---- canvas 雷达图 ----
function drawRadar(id, vals, labels){
  const c = $(id); if(!c) return;
  const dpr = window.devicePixelRatio || 1;
  const W = 320, H = 320; c.width = W*dpr; c.height = H*dpr; c.style.width=W+"px"; c.style.height=H+"px";
  const ctx = c.getContext("2d"); ctx.scale(dpr, dpr);
  const cx = W/2, cy = H/2, R = 110;
  const n = vals.length;
  ctx.clearRect(0,0,W,H);
  // 网格
  for(let g=1; g<=5; g++){
    ctx.beginPath();
    for(let i=0;i<n;i++){
      const a = -Math.PI/2 + i*2*Math.PI/n;
      const r = R*g/5;
      const x = cx+Math.cos(a)*r, y = cy+Math.sin(a)*r;
      i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
    }
    ctx.closePath();
    ctx.strokeStyle = "#262b3c"; ctx.lineWidth = 1; ctx.stroke();
  }
  // 轴
  for(let i=0;i<n;i++){
    const a = -Math.PI/2 + i*2*Math.PI/n;
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(cx+Math.cos(a)*R, cy+Math.sin(a)*R);
    ctx.strokeStyle = "#262b3c"; ctx.stroke();
  }
  // 数据
  ctx.beginPath();
  for(let i=0;i<n;i++){
    const a = -Math.PI/2 + i*2*Math.PI/n;
    const r = R * Math.max(0, Math.min(10, vals[i])) / 10;
    const x = cx+Math.cos(a)*r, y = cy+Math.sin(a)*r;
    i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
  }
  ctx.closePath();
  ctx.fillStyle = "rgba(91,140,255,0.18)"; ctx.fill();
  ctx.strokeStyle = "#5b8cff"; ctx.lineWidth = 2; ctx.stroke();
  // 点
  for(let i=0;i<n;i++){
    const a = -Math.PI/2 + i*2*Math.PI/n;
    const r = R * Math.max(0, Math.min(10, vals[i])) / 10;
    ctx.beginPath(); ctx.arc(cx+Math.cos(a)*r, cy+Math.sin(a)*r, 3, 0, 2*Math.PI);
    ctx.fillStyle = "#5b8cff"; ctx.fill();
  }
  // 标签
  ctx.fillStyle = "#8b93a7"; ctx.font = "11px sans-serif"; ctx.textAlign = "center";
  for(let i=0;i<n;i++){
    const a = -Math.PI/2 + i*2*Math.PI/n;
    const x = cx+Math.cos(a)*(R+18), y = cy+Math.sin(a)*(R+18)+4;
    ctx.fillText(labels[i], x, y);
    ctx.fillStyle = "#e6e9f0"; ctx.fillText(vals[i].toFixed(1), x, y+14); ctx.fillStyle = "#8b93a7";
  }
}

// ---- canvas 版本曲线 ----
function drawCurve(id, scores){
  const c = $(id); if(!c) return;
  const dpr = window.devicePixelRatio || 1;
  const W = 320, H = 200; c.width = W*dpr; c.height = H*dpr; c.style.width=W+"px"; c.style.height=H+"px";
  const ctx = c.getContext("2d"); ctx.scale(dpr, dpr);
  ctx.clearRect(0,0,W,H);
  const pad = 30;
  if(!scores.length){ ctx.fillStyle="#6b7280"; ctx.font="12px sans-serif"; ctx.textAlign="center"; ctx.fillText("暂无版本数据", W/2, H/2); return; }
  const maxV = 10, minV = Math.max(0, Math.min(...scores)-1);
  // 网格 + Y 轴
  ctx.strokeStyle = "#262b3c"; ctx.fillStyle = "#6b7280"; ctx.font = "10px sans-serif"; ctx.textAlign = "right";
  for(let v=minV; v<=maxV; v+=(maxV-minV>5?2:1)){
    const y = H-pad - (v-minV)/(maxV-minV)*(H-2*pad);
    ctx.beginPath(); ctx.moveTo(pad,y); ctx.lineTo(W-pad,y); ctx.stroke();
    ctx.fillText(v.toFixed(0), pad-4, y+3);
  }
  // 8.0 阈值线
  if(minV <= 8 && 8 <= maxV){
    const y = H-pad - (8-minV)/(maxV-minV)*(H-2*pad);
    ctx.strokeStyle = "#3ecf8e"; ctx.setLineDash([4,3]); ctx.beginPath(); ctx.moveTo(pad,y); ctx.lineTo(W-pad,y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = "#3ecf8e"; ctx.textAlign = "left"; ctx.fillText("8.0 阈值", pad+4, y-3);
  }
  // 数据
  const xs = scores.map((_,i)=> scores.length===1 ? W/2 : pad + i*(W-2*pad)/(scores.length-1));
  const ys = scores.map(s => H-pad - (s-minV)/(maxV-minV)*(H-2*pad));
  ctx.strokeStyle = "#5b8cff"; ctx.lineWidth = 2; ctx.beginPath();
  xs.forEach((x,i)=> i===0?ctx.moveTo(x,ys[i]):ctx.lineTo(x,ys[i]));
  ctx.stroke();
  xs.forEach((x,i)=>{
    ctx.beginPath(); ctx.arc(x,ys[i],3,0,2*Math.PI); ctx.fillStyle="#5b8cff"; ctx.fill();
    ctx.fillStyle="#e6e9f0"; ctx.font="10px sans-serif"; ctx.textAlign="center"; ctx.fillText(scores[i].toFixed(1), x, ys[i]-8);
  });
  ctx.fillStyle="#6b7280"; ctx.textAlign="center";
  xs.forEach((x,i)=> ctx.fillText("v"+i, x, H-pad+14));
}
"""

JS_MD = r"""// ---- 轻量 Markdown 渲染 ----
function md2html(md){
  let h = esc(md);
  // 代码块先抽成占位符，内容**全程保持转义态**。
  // 历史教训：旧版为"让代码里的 < 显示出来"做了 c.replace(/&lt;/g,"<") 反向还原，
  // 结果报告正文（含任务原文与模型输出）只要出现在围栏里就能把 <img onerror=...>
  // 原样送进 innerHTML —— 存储型 XSS：任何能提交任务的人都能给控制台投毒。
  // 代码块是纯文本展示场景，转义后原样输出才是正确行为，不需要还原。
  // 抽成占位符还有个副作用收益：围栏里的 ** / ` 不再被当成行内标记二次渲染。
  const blocks = [];
  h = h.replace(/```(\w*)\n([\s\S]*?)```/g, (_,l,c)=>{
    blocks.push(c);
    return "%%PMCB" + (blocks.length - 1) + "%%";
  });
  // 表格
  h = h.replace(/((?:^\|.*\|\n?)+)/m, tbl => {
    const rows = tbl.trim().split("\n").map(r=>r.trim());
    if(rows.length<2) return tbl;
    const head = rows[0].split("|").filter((_,i,a)=>i>0&&i<a.length-1).map(c=>`<th>${c.trim()}</th>`).join("");
    const body = rows.slice(2).map(r=>`<tr>${r.split("|").filter((_,i,a)=>i>0&&i<a.length-1).map(c=>`<td>${c.trim()}</td>`).join("")}</tr>`).join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  });
  h = h.replace(/^### (.*)$/gm, '<h3>$1</h3>');
  h = h.replace(/^## (.*)$/gm, '<h2>$1</h2>');
  h = h.replace(/^# (.*)$/gm, '<h1>$1</h1>');
  h = h.replace(/^> (.*)$/gm, '<blockquote>$1</blockquote>');
  h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
  h = h.replace(/^\s*[-*] (.*)$/gm, '<li>$1</li>');
  h = h.replace(/(<li>[\s\S]*?<\/li>)/g, m=>`<ul>${m}</ul>`);
  h = h.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  // 还原代码块（仍是转义态，直接进 <pre> 显示为字面文本）
  h = h.replace(/%%PMCB(\d+)%%/g, (_,i)=>`<pre>${blocks[+i]}</pre>`);
  return h;
}
"""

JS_TOOLS = r"""// ---- 工具 ----
function copyText(btn, key){
  const val = copyStore[key] || "";
  navigator.clipboard.writeText(val).then(()=>{
    const o = btn.textContent; btn.textContent = "已复制 ✓";
    setTimeout(()=>btn.textContent=o, 1400);
  });
}
"""

JS_RACE = r"""// ---- 赛马 ----
$("raceBtn").onclick = async () => {
  const task = $("chatInput").value.trim();
  if(!task){ $("ciErr").textContent = "先在输入框写需求，再加入赛马"; return; }
  raceQueue.push({task, target_model: $("targetModel").value.trim() || "未指定", n_test_cases: parseInt($("optCases").value)||2, max_iterations: parseInt($("optIter").value)||2});
  $("chatInput").value = "";
  $("ciErr").textContent = `✅ 已入赛马队列（${raceQueue.length} 个），再写一个需求并点 🏁 发起`;
  if(raceQueue.length >= 2){
    try{
      const d = await api("/api/race","POST",{name:"控制台赛马", tasks: raceQueue});
      raceQueue.length = 0;
      $("ciErr").textContent = `🏁 赛马 ${d.race_id} 已启动`;
      // 把赛马的 run 加入轮询
      const rs = await api(`/api/race/${d.race_id}`);
      Object.entries(rs.runs||{}).forEach(([rid,s])=>{ runs[rid] = Object.assign({task:`[赛马] ${task.slice(0,30)}`, trace:[], evaluations:[], prompt_version_list:[]}, s); });
      renderRunList(); ensurePolling();
    }catch(e){ $("ciErr").textContent = "赛马失败：" + e.message; }
  }
};
"""

JS_EVENTS = r"""// ---- 委托事件 ----
// CSP 收紧后内联事件属性会被浏览器拦掉（script-src 不再是 'unsafe-inline'），
// 改由 data-* + 事件委托统一处理。
// 注意：这里用 addEventListener / el.onclick = fn 属性赋值都没问题 ——
// 被 CSP 拦的是 HTML 里的事件处理属性，不是 JS 侧的函数绑定。
document.addEventListener("click", e => {
  const el = e.target instanceof Element ? e.target : null;
  if(!el) return;
  const cp = el.closest("[data-copy]"), rn = el.closest("[data-run]"), rp = el.closest("[data-report]");
  if(cp){ copyText(cp, cp.dataset.copy); return; }
  if(rn){ selectRun(rn.dataset.run); return; }
  if(rp){ loadReport(rp.dataset.report); }
});
document.addEventListener("change", e => {
  const el = e.target instanceof Element ? e.target.closest("[data-pv]") : null;
  if(!el) return;
  if(el.dataset.pv === "A") pvA = +el.value; else pvB = +el.value;
  renderPromptView(runs[activeRid]);
});

// ---- 事件 ----
$("sendBtn").onclick = () => submitTask($("chatInput").value);
$("chatInput").addEventListener("keydown", e => {
  if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); submitTask($("chatInput").value); }
});
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  t.classList.add("active"); curTab = t.dataset.tab;
  document.querySelectorAll(".tab-pane").forEach(p=>p.classList.remove("active"));
  $("pane-"+curTab).classList.add("active");
  if(activeRid) renderActive();
  if(curTab==="history") renderHistory();
});
"""

JS_HISTORY = r"""// ---- 历史面板：跨任务运行聚合 + 同任务 Δ 显著性 ----
let historyLoaded = false;
async function renderHistory(){
  const v = $("historyView");
  v.innerHTML = '<div class="empty-ws">加载中…</div>';
  try{
    const d = await api("/api/history");
    historyLoaded = true;
    let html = "";
    if(d.task_groups && d.task_groups.length){
      html += '<h3>同任务跨 run 显著性（Δ 均值 ± 95% CI）</h3>';
      for(const g of d.task_groups){
        const mark = g.significant ? "✅ 显著" : "⚠️ 不显著";
        html += `<div class="hist-group">` +
          `<b>${mark}</b> Δ均值 <b>${g.delta_mean >= 0 ? "+" : ""}${g.delta_mean}</b>` +
          ` CI [${g.delta_ci95[0]}, ${g.delta_ci95[1]}]（n=${g.n_runs}${g.note ? "，" + g.note : ""}）` +
          `<div class="u-dim">${g.task}</div></div>`;
      }
      html += "<h3>最近运行</h3>";
    }
    html += '<table class="hist-table"><tr><th>日期</th><th>run_id</th><th>状态</th><th>基线</th><th>优化</th><th>Δ</th><th>调用</th><th>任务</th></tr>';
    for(const r of d.runs){
      const ba = r.base_avg != null ? r.base_avg.toFixed(2) : "-";
      const oa = r.opt_avg != null ? r.opt_avg.toFixed(2) : "-";
      const dl = r.delta != null ? (r.delta >= 0 ? "+" : "") + r.delta.toFixed(2) : "-";
      html += `<tr><td>${r.mtime}</td><td>${r.run_id}</td><td>${r.status}</td><td>${ba}</td><td>${oa}</td><td>${dl}</td><td>${r.llm_calls}</td><td>${r.task}</td></tr>`;
    }
    html += "</table>";
    v.innerHTML = html;
  }catch(e){
    v.innerHTML = '<div class="empty-ws">历史加载失败：' + e.message + "</div>";
  }
}
"""

JS_INIT = r"""// 初始
window.addEventListener("resize", () => { if(activeRid) renderScoreView(runs[activeRid]); });"""
