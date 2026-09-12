"""
PromptMaster Web 控制台 —— Agent 风格富 SPA（单 HTML，内联 CSS/JS/canvas，零外部依赖）。

布局：
- 左栏：任务/对话历史（每个 run 一条，带状态徽章 + 实时分数）
- 中栏：选中任务后的四标签工作区
  · 对话：提需求（聊天输入）→ agent 实时反馈卡片 → 最终提示词 + 报告渲染
  · 流水线：节点级 trace 时间线（clarify→optimize→...→report），按迭代分组
  · 提示词：版本切换 + 全文查看 + 双版本 diff + 一键复制
  · 评分：维度雷达图（canvas）+ 版本分数曲线（canvas）+ 聚合/基线 Δ/盲评结论
- 底部：聊天输入条（agent 感）

由 server.py 的 GET / 路由提供。后端 /api/status 已暴露 trace/evaluations/prompt_version_list
等富字段（见 scheduler._progress_of），本页直接消费。
"""

WEB_CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PromptMaster · Agent</title>
<style nonce="__CSP_NONCE__">
  :root{
    --bg:#0b0d13; --bg2:#0f1218; --panel:#161922; --panel2:#1c2030; --border:#262b3c; --border2:#333a4f;
    --text:#e6e9f0; --muted:#8b93a7; --dim:#6b7280; --accent:#5b8cff; --accent2:#7c5cff;
    --ok:#3ecf8e; --warn:#ffb454; --bad:#ff6b6b; --gold:#ffd166;
    --mono:"JetBrains Mono","Cascadia Code",Consolas,monospace;
    --sans:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px;overflow:hidden}
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:#2a3040;border-radius:4px}
  ::-webkit-scrollbar-track{background:transparent}

  /* ---- 顶栏 ---- */
  .topbar{height:48px;display:flex;align-items:center;gap:14px;padding:0 18px;
    background:var(--bg2);border-bottom:1px solid var(--border);position:relative;z-index:5}
  .logo{display:flex;align-items:center;gap:8px;font-weight:600;font-size:15px}
  .logo .dot{width:10px;height:10px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2))}
  .topbar .spacer{flex:1}
  .health{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
  .health .led{width:7px;height:7px;border-radius:50%;background:var(--dim)}
  .health.ok .led{background:var(--ok);box-shadow:0 0 6px var(--ok)}
  .topbar button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:7px 14px;font-size:12px;cursor:pointer;font-family:inherit}
  .topbar button:hover{filter:brightness(1.15)}

  /* ---- 主布局 ---- */
  .layout{display:grid;grid-template-columns:280px 1fr;height:calc(100% - 48px)}
  .sidebar{background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
  .sidebar h3{padding:14px 16px 8px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
  .run-list{flex:1;overflow-y:auto;padding:0 8px 8px}
  .run-item{padding:10px 12px;border-radius:8px;cursor:pointer;margin-bottom:4px;border:1px solid transparent}
  .run-item:hover{background:var(--panel)}
  .run-item.active{background:var(--panel);border-color:var(--border2)}
  .run-item .ri-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}
  .run-item .ri-id{font-family:var(--mono);font-size:11px;color:var(--muted)}
  .run-item .ri-task{font-size:12px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .badge{font-size:10px;padding:2px 7px;border-radius:10px;background:#2a3040;font-weight:500}
  .badge.running{background:#25314d;color:#8ab4ff}
  .badge.pending{background:#2a3040;color:var(--muted)}
  .badge.passed{background:#123524;color:var(--ok)}
  .badge.failed,.badge.max_iterations{background:#3d1f24;color:#ff8080}
  .badge.early_stopped{background:#3a2e17;color:var(--warn)}
  .run-item .ri-meta{font-size:11px;color:var(--muted);display:flex;gap:10px;margin-top:4px}
  .sidebar .newbar{padding:10px 12px;border-top:1px solid var(--border)}
  .sidebar .newbar input{width:100%;background:#0d0f15;border:1px solid var(--border);color:var(--text);
    border-radius:6px;padding:8px 10px;font-size:12px;font-family:inherit}

  /* ---- 工作区 ---- */
  .workspace{display:flex;flex-direction:column;overflow:hidden;background:var(--bg)}
  .tabs{display:flex;gap:2px;padding:0 16px;background:var(--bg2);border-bottom:1px solid var(--border);flex:0 0 40px;align-items:center}
  .tab{padding:10px 16px;font-size:13px;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;user-select:none}
  .tab:hover{color:var(--text)}
  .tab.active{color:var(--text);border-color:var(--accent)}
  .tab .cnt{font-size:10px;background:var(--panel2);padding:1px 5px;border-radius:8px;margin-left:4px;color:var(--muted)}
  .tab-pane{flex:1;overflow-y:auto;padding:20px 28px;display:none}
  .tab-pane.active{display:block}
  .empty-ws{color:var(--muted);text-align:center;padding:80px 0;font-size:13px}

  /* ---- 对话 ---- */
  .chat-flow{display:flex;flex-direction:column;gap:14px;max-width:880px;margin:0 auto;padding-bottom:120px}
  .msg{display:flex;gap:10px}
  .msg .ava{width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:13px;flex:0 0 28px}
  .msg.user .ava{background:linear-gradient(135deg,#3a4f7d,#5b8cff)}
  .msg.bot .ava{background:linear-gradient(135deg,var(--accent2),#5b8cff)}
  .msg .body{flex:1;min-width:0}
  .msg .who{font-size:11px;color:var(--muted);margin-bottom:4px}
  .msg .content{font-size:13px;line-height:1.6;word-break:break-word}
  .bubble-card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;margin-top:6px}
  .bubble-card .bc-row{display:flex;justify-content:space-between;font-size:12px;padding:3px 0;color:var(--muted)}
  .bubble-card .bc-row b{color:var(--text);font-weight:500}
  .prompt-box{background:#0d0f15;border:1px solid var(--border);border-radius:8px;padding:12px;
    font-family:var(--mono);font-size:12px;white-space:pre-wrap;line-height:1.55;max-height:320px;overflow-y:auto;margin-top:8px}
  .copybtn{float:right;background:var(--panel2);color:var(--muted);border:1px solid var(--border);border-radius:5px;
    padding:3px 9px;font-size:11px;cursor:pointer;font-family:inherit}
  .copybtn:hover{color:var(--text)}
  .progress-line{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);margin-top:6px}
  .spinner{width:13px;height:13px;border:2px solid var(--border2);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}

  /* ---- 流水线 ---- */
  .pipeline{max-width:920px;margin:0 auto}
  .iter-group{margin-bottom:22px}
  .iter-group .ig-head{font-size:12px;color:var(--muted);margin-bottom:10px;display:flex;align-items:center;gap:8px}
  .iter-group .ig-head .pill{background:var(--panel2);padding:2px 9px;border-radius:10px;font-size:11px;color:var(--accent)}
  .node-row{display:flex;align-items:flex-start;gap:12px;padding:6px 0;position:relative}
  .node-row::before{content:"";position:absolute;left:13px;top:22px;bottom:-6px;width:2px;background:var(--border)}
  .node-row:last-child::before{display:none}
  .node-ic{width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px;flex:0 0 28px;z-index:1}
  .node-ic.done{background:#123524;color:var(--ok)}
  .node-ic.running{background:#25314d;color:#8ab4ff}
  .node-ic.skip{background:var(--panel2);color:var(--muted)}
  .node-ic.fail{background:#3d1f24;color:var(--bad)}
  .node-info{flex:1;min-width:0}
  .node-info .ni-name{font-size:13px;font-weight:500}
  .node-info .ni-event{font-size:11px;color:var(--muted);font-family:var(--mono)}
  .node-info .ni-meta{font-size:11px;color:var(--muted);margin-top:2px;display:flex;gap:12px;flex-wrap:wrap}

  /* ---- 提示词 ---- */
  .pv-bar{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
  .pv-bar select{background:#0d0f15;border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 10px;font-size:12px;font-family:inherit}
  .pv-cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .pv-col h4{font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:6px}
  .diff{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border);border:1px solid var(--border);border-radius:8px;overflow:hidden}
  .diff .dc{background:#0d0f15;padding:10px;font-family:var(--mono);font-size:11px;white-space:pre-wrap;line-height:1.5;overflow-x:auto}
  .diff .dc.add{color:var(--ok)}
  .diff .dc.del{color:var(--bad)}
  .diff .dc.eq{color:var(--muted)}

  /* ---- 评分 ---- */
  .score-grid{display:grid;grid-template-columns:340px 1fr;gap:20px;max-width:980px;margin:0 auto}
  .score-cards{display:flex;flex-direction:column;gap:12px}
  .stat-card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}
  .stat-card h4{font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:8px}
  .stat-card .big{font-size:28px;font-weight:600;font-family:var(--mono)}
  .stat-card .big.ok{color:var(--ok)}
  .stat-card .big.bad{color:var(--bad)}
  .stat-card .sub{font-size:11px;color:var(--muted);margin-top:4px}
  .kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:12px}
  .kv .k{color:var(--muted)}
  .chart-box{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}
  .chart-box h4{font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:8px}
  canvas{display:block;width:100%}

  /* ---- 报告 markdown ---- */
  .md{font-size:13px;line-height:1.7}
  .md h1{font-size:17px;margin:14px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--border)}
  .md h2{font-size:15px;margin:14px 0 6px;color:var(--accent)}
  .md h3{font-size:13px;margin:12px 0 4px}
  .md table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}
  .md th,.md td{border:1px solid var(--border);padding:5px 9px;text-align:left}
  .md th{background:var(--panel2);color:var(--muted);font-weight:500}
  .md code{background:#0d0f15;padding:1px 5px;border-radius:4px;font-family:var(--mono);font-size:11px}
  .md pre{background:#0d0f15;border:1px solid var(--border);border-radius:6px;padding:10px;overflow-x:auto;font-family:var(--mono);font-size:11px;margin:8px 0}
  .md ul,.md ol{padding-left:20px;margin:6px 0}
  .md li{margin:3px 0}
  .md blockquote{border-left:3px solid var(--accent);padding-left:12px;color:var(--muted);margin:8px 0}

  /* ---- 底部聊天输入 ---- */
  .chat-input{position:sticky;bottom:0;background:var(--bg2);border-top:1px solid var(--border);padding:12px 20px}
  .chat-input .ci-row{display:flex;gap:10px;align-items:flex-end;max-width:880px;margin:0 auto}
  .chat-input textarea{flex:1;background:var(--panel);border:1px solid var(--border2);color:var(--text);
    border-radius:10px;padding:11px 14px;font-size:13px;font-family:inherit;resize:none;min-height:44px;max-height:160px}
  .chat-input textarea:focus{outline:0;border-color:var(--accent)}
  .chat-input .send{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:0;border-radius:10px;
    width:44px;height:44px;font-size:18px;cursor:pointer;display:flex;align-items:center;justify-content:center}
  .chat-input .send:disabled{opacity:.4;cursor:not-allowed}
  .ci-opts{display:flex;gap:14px;align-items:center;margin-top:6px;font-size:11px;color:var(--muted)}
  .ci-opts input{width:60px;background:#0d0f15;border:1px solid var(--border);color:var(--text);border-radius:4px;padding:3px 6px;font-size:11px;font-family:inherit;text-align:center}
  .ci-opts label{display:flex;align-items:center;gap:4px}
  .err-text{color:var(--bad);font-size:11px;margin-top:4px}

  /* ---- 工具类：替代内联 style 属性（CSP 收紧后 style 属性会被浏览器拦掉）---- */
  .u-muted{color:var(--muted)}
  .u-warn{color:var(--warn)}
  .u-bad{color:var(--bad)}
  .u-text{color:var(--text)}
  .u-brand-sub{color:var(--muted);font-weight:400;font-size:12px}
  .u-empty-side{color:var(--muted);font-size:12px;padding:12px 4px}
  .u-mt8{margin-top:8px}
  .u-mt14{margin-top:14px}
  .u-nomax{max-height:none}
  .u-w100{width:100%}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo"><span class="dot"></span>PromptMaster<span class="u-brand-sub">· Agent</span></div>
  <div class="spacer"></div>
  <div class="health" id="health"><span class="led"></span><span id="healthText">检测中</span></div>
  <button id="raceBtn">🏁 批量赛马</button>
</div>

<div class="layout">
  <!-- 左栏：任务历史 -->
  <div class="sidebar">
    <h3>任务历史</h3>
    <div class="run-list" id="runList"><div class="u-empty-side">还没有任务，在下方输入需求开始</div></div>
    <div class="newbar">
      <input id="targetModel" value="DeepSeek-V4-Flash" placeholder="目标模型">
    </div>
  </div>

  <!-- 中栏：工作区 -->
  <div class="workspace">
    <div class="tabs" id="tabs">
      <div class="tab active" data-tab="chat">对话</div>
      <div class="tab" data-tab="pipeline">流水线 <span class="cnt" id="cntTrace">0</span></div>
      <div class="tab" data-tab="prompt">提示词 <span class="cnt" id="cntPV">0</span></div>
      <div class="tab" data-tab="score">评分</div>
    </div>

    <div class="tab-pane active" id="pane-chat">
      <div class="chat-flow" id="chatFlow"><div class="empty-ws">在底部输入你的提示词需求，agent 会自动澄清、生成、测试、评估、迭代优化</div></div>
    </div>
    <div class="tab-pane" id="pane-pipeline"><div class="pipeline" id="pipeline"><div class="empty-ws">提交任务后，节点级执行轨迹会出现在这里</div></div></div>
    <div class="tab-pane" id="pane-prompt"><div id="promptView"><div class="empty-ws">生成的提示词版本与 diff 会出现在这里</div></div></div>
    <div class="tab-pane" id="pane-score"><div id="scoreView"><div class="empty-ws">评分雷达图、版本曲线、基线 Δ 与盲评结论会出现在这里</div></div></div>

    <!-- 底部聊天输入 -->
    <div class="chat-input">
      <div class="ci-row">
        <textarea id="chatInput" placeholder="描述你想要的提示词… 例如：让 AI 解析销售数据 CSV 并给出区域排名与同比结论"></textarea>
        <button class="send" id="sendBtn" title="提交优化任务">➤</button>
      </div>
      <div class="ci-opts">
        <label>用例数 <input id="optCases" type="number" value="2" min="1" max="8"></label>
        <label>最大迭代 <input id="optIter" type="number" value="2" min="0" max="10"></label>
        <span id="optHint" class="u-muted">· Enter 发送 / Shift+Enter 换行</span>
      </div>
      <div class="err-text" id="ciErr"></div>
    </div>
  </div>
</div>

<script nonce="__CSP_NONCE__">
"use strict";
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

// ========== 对话标签 ==========
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

// ========== 流水线标签 ==========
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

// ========== 提示词标签 ==========
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

// ========== 评分标签 ==========
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

// ---- canvas 雷达图 ----
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

// ---- 轻量 Markdown 渲染 ----
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

// ---- 工具 ----
function copyText(btn, key){
  const val = copyStore[key] || "";
  navigator.clipboard.writeText(val).then(()=>{
    const o = btn.textContent; btn.textContent = "已复制 ✓";
    setTimeout(()=>btn.textContent=o, 1400);
  });
}

// ---- 赛马 ----
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

// ---- 委托事件 ----
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
});

// 初始
window.addEventListener("resize", () => { if(activeRid) renderScoreView(runs[activeRid]); });
</script>
</body>
</html>
"""
