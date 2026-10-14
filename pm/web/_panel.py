"""PromptMaster 面板（插件形态）—— 单文件紧凑 UI，与富控制台同源但形状不同。

为什么再做一个页面：`pm/web/` 那个富控制台是"跑完整轮优化"的作业台（左栏任务历史 +
四个标签 + canvas 雷达），它假设你已经有 server、有 Key、愿意等 40~80 次调用。
而本产品现在**当天就能依赖**的那三件事是零调用的：体检一份提示词、数清一份考卷有几票、
差分两版改了什么。这三件事在富控制台里根本没有入口 —— 它们只在 CLI 与 MCP 里。
本页就是给这三件事一个能被"点开就用"的形状，附带一条只在 Key 就绪时才亮的优化闭环。

安全约定与 `pm/web/` 完全一致（不是复制样式，是复制**约束**）：
- 零外部资源 → CSP 才能用 `default-src 'self'`；
- 无内联事件属性、无 `style="..."` 属性 → nonce CSP 下不会被拦（样式由 CSSOM 设置）；
- 任何进 innerHTML 的文本必须先过 `esc()`（面板显示的是用户粘贴的提示词与模型输出）。

`frame-ancestors 'none'` 是 server 的统一响应头，所以本页**不能**被 iframe 嵌进别的应用；
它的"插件"含义是"一条命令起一个小窗口"，不是"可嵌入组件"。要嵌得改 CSP，那是另一笔账。
"""

from __future__ import annotations

PANEL_CSS = """  :root{
    --bg:#0f1115; --card:#171a21; --line:#262b36; --ink:#e6e9ef; --muted:#8b94a7;
    --ok:#3fb27f; --warn:#d9a441; --bad:#e05c5c; --accent:#5b8cff; --chip:#20252f;
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f5f6f8; --card:#ffffff; --line:#e2e5eb; --ink:#1a1d24; --muted:#6b7385;
           --ok:#1f8a5b; --warn:#a06d13; --bad:#c0392b; --accent:#2f6bd6; --chip:#eef1f6; }
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--ink)}
  body{font:14px/1.55 ui-sans-serif,system-ui,"Segoe UI","Microsoft YaHei",sans-serif;
       display:flex;justify-content:center;padding:14px 10px 40px}
  .p-root{width:100%;max-width:470px}
  .p-head{display:flex;align-items:center;gap:8px;margin:0 0 12px}
  .p-brand{font-weight:650;letter-spacing:.2px}
  .p-brand .u-sub{color:var(--muted);font-weight:400;font-size:12px;margin-left:6px}
  .p-leds{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}
  .led{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;color:var(--muted);
       background:var(--chip);border:1px solid var(--line);border-radius:999px;padding:2px 8px}
  .led .dot{width:7px;height:7px;border-radius:50%;background:var(--muted)}
  .led.ok .dot{background:var(--ok)} .led.bad .dot{background:var(--bad)}
  .led.warn .dot{background:var(--warn)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:12px 13px;margin:0 0 11px}
  .card h2{margin:0 0 2px;font-size:14px;font-weight:650}
  .card .hint{color:var(--muted);font-size:11.5px;margin:0 0 9px}
  .card.off{opacity:.62}
  textarea,select,input[type=text]{width:100%;background:var(--bg);color:var(--ink);
    border:1px solid var(--line);border-radius:8px;padding:7px 8px;font:12.5px/1.5 ui-monospace,
    Consolas,"Courier New",monospace;resize:vertical}
  textarea{min-height:88px}
  .ta-tall{min-height:120px}
  .row{display:flex;gap:7px;align-items:center;margin:8px 0 0;flex-wrap:wrap}
  .row .grow{flex:1 1 140px;min-width:120px}
  button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:6px 12px;
    font-size:12.5px;font-weight:600;cursor:pointer}
  button.ghost{background:var(--chip);color:var(--ink);border:1px solid var(--line);font-weight:500}
  button:disabled{opacity:.45;cursor:not-allowed}
  .out{margin-top:10px;border-top:1px dashed var(--line);padding-top:9px}
  .out:empty{display:none}
  .verdict{font-size:12.5px;font-weight:650;margin:0 0 7px}
  .verdict.ok{color:var(--ok)} .verdict.bad{color:var(--bad)} .verdict.na{color:var(--muted)}
  ul.f{margin:0;padding:0;list-style:none}
  ul.f li{border-left:3px solid var(--bad);background:var(--chip);border-radius:0 7px 7px 0;
    padding:6px 9px;margin:0 0 6px}
  ul.f li.judge{border-left-color:var(--warn)}
  .fcode{font:11.5px ui-monospace,Consolas,monospace;color:var(--accent);display:block}
  .fdetail{color:var(--ink);font-size:12.3px}
  .kv{display:flex;flex-wrap:wrap;gap:4px 14px;margin:0 0 8px;font-size:12px}
  .kv b{font-variant-numeric:tabular-nums}
  .kv span{color:var(--muted)}
  .bar{display:flex;height:11px;border-radius:6px;overflow:hidden;background:var(--chip);margin:2px 0 6px}
  .bar i{display:block;height:100%}
  .bar .strong{background:var(--ok)} .bar .echo{background:var(--accent)}
  .bar .judge{background:var(--warn)} .bar .dead{background:var(--bad)}
  .legend{display:flex;gap:10px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin:0 0 7px}
  .legend .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px}
  .dot.strong{background:var(--ok)} .dot.echo{background:var(--accent)}
  .dot.judge{background:var(--warn)} .dot.dead{background:var(--bad)}
  pre.uni{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;
    font-size:11.5px;line-height:1.45;overflow:auto;max-height:260px;margin:8px 0 0;
    white-space:pre-wrap;word-break:break-word}
  .err{color:var(--bad);font-size:12.3px;margin-top:8px;white-space:pre-wrap}
  .muted{color:var(--muted)}
  .p-foot{color:var(--muted);font-size:11px;text-align:center;margin-top:14px}
  .p-foot code{font-family:ui-monospace,Consolas,monospace}
"""

PANEL_BODY = """<body>
<div class="p-root">

  <div class="p-head">
    <div class="p-brand">PromptMaster<span class="u-sub">· 面板</span></div>
    <div class="p-leds">
      <span class="led" id="ledBack"><span class="dot"></span><span id="ledBackT">后端</span></span>
      <span class="led" id="ledCli"><span class="dot"></span><span id="ledCliT">零调用</span></span>
      <span class="led" id="ledKey"><span class="dot"></span><span id="ledKeyT">Key</span></span>
    </div>
  </div>

  <div class="card" id="cardConn">
    <h2>连接</h2>
    <p class="hint">设了 <code>PM_API_TOKEN</code> 时把 token 填在这里，否则所有请求会被自家门禁 401 挡掉
      （只存 sessionStorage，关页即清）。</p>
    <div class="row">
      <input type="text" id="tokenIn" class="grow" placeholder="X-API-Key（未设 token 时留空）" autocomplete="off">
      <button id="tokenBtn" class="ghost">保存并重检</button>
    </div>
  </div>

  <div class="card">
    <h2>① 提示词体检</h2>
    <p class="hint">零调用、不联网。回答「这版会不会出事」，不回答「这版能不能完成任务」。</p>
    <textarea id="promptIn" placeholder="粘贴你已有的那一版提示词…"></textarea>
    <div class="row">
      <button id="checkBtn">体检</button>
      <button id="checkClearBtn" class="ghost">清空</button>
      <span class="grow muted" id="checkMeta"></span>
    </div>
    <div class="out" id="checkOut"></div>
    <div class="err" id="checkErr"></div>
  </div>

  <div class="card">
    <h2>② 考卷有几票</h2>
    <p class="hint">花钱之前先量考卷：数清这份用例集里有几票真能否决。选自带模板或直接粘你的 JSON 数组。</p>
    <div class="row">
      <select id="tplSel" class="grow"><option value="">（载入中…）</option></select>
      <button id="casesBtn">体检考卷</button>
    </div>
    <textarea id="casesIn" class="ta-tall" placeholder='[{"input":"…","expected":"…","assert_mode":"rule"}]'></textarea>
    <div class="out" id="casesOut"></div>
    <div class="err" id="casesErr"></div>
  </div>

  <div class="card">
    <h2>③ 两版差分</h2>
    <p class="hint">零调用。只认「本次新引入」——修好不判失败；改完还是同一份就不判。</p>
    <textarea id="diffA" placeholder="改动前那一版"></textarea>
    <div class="row"><span class="muted">↓ 改成</span></div>
    <textarea id="diffB" placeholder="改动后那一版"></textarea>
    <div class="row">
      <button id="diffBtn">差分</button>
      <button id="diffSwapBtn" class="ghost">用①的正文填「前」</button>
    </div>
    <div class="out" id="diffOut"></div>
    <div class="err" id="diffErr"></div>
  </div>

  <div class="card off" id="cardRun">
    <h2>④ 优化闭环</h2>
    <p class="hint">要真 Key、一轮约 60 分钟 / 70~80 次调用。它给的是<b>自评 Δ 与成对盲评</b>；
      截至本版，「优化后更好」按项目自己的判据仍 <b>0 例确证</b> —— 请看结论别只看分数。</p>
    <textarea id="taskIn" placeholder="一句话需求，例如：让 AI 解析销售 CSV 并给出区域排名与同比结论"></textarea>
    <div class="row">
      <input type="text" id="modelIn" class="grow" placeholder="目标模型（可空）">
      <button id="runBtn">提交</button>
      <button id="reportBtn" class="ghost" disabled>取报告</button>
    </div>
    <div class="row">
      <span class="muted" id="runMeta">还没有任务</span>
    </div>
    <div class="out" id="runOut"></div>
    <div class="out" id="runReport"></div>
    <div class="err" id="runErr"></div>
  </div>

  <div class="p-foot">
    退出码协议 0 达标 / 1 未达标但已交付 / 2 参数或配置 / 3 运行失败 ·
    同源 CLI：<code>run.py check|diff</code> · MCP 13 工具
  </div>
</div>
"""

PANEL_JS = r""""use strict";
// ---- 基础 ----
function $(id){ return document.getElementById(id); }
function esc(s){
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function token(){ return sessionStorage.getItem("pm.panel.token") || ""; }
async function api(path, method, body){
  const headers = {"Content-Type": "application/json"};
  if (token()) headers["X-API-Key"] = token();
  const r = await fetch(path, {method: method || "GET", headers: headers,
    body: body ? JSON.stringify(body) : undefined});
  let j = null;
  const txt = await r.text();
  try { j = JSON.parse(txt); } catch (_) { j = null; }
  if (!r.ok){
    let d = (j && (j.detail || j.error)) || txt.slice(0, 200) || r.statusText;
    if (typeof d !== "string") d = JSON.stringify(d);
    if (r.status === 401) d = "401 未授权：" + d + "（顶部「连接」里填 X-API-Key）";
    throw new Error(d);
  }
  if (j === null) throw new Error("响应不是 JSON（HTTP " + r.status + "）");
  return j;
}
function led(idEl, idText, cls, text){
  $(idEl).className = "led" + (cls ? " " + cls : "");
  $(idText).textContent = text;
}
function showErr(id, e){ $(id).textContent = (e && e.message) ? e.message : String(e); }
function clearErr(id){ $(id).textContent = ""; }
function n(x){ return (x == null ? 0 : Number(x)) || 0; }

// ---- 连接自检 ----
async function loadCaps(){
  clearErr("checkErr");
  try{
    const c = await api("/api/panel/capabilities");
    led("ledBack", "ledBackT", "ok", "后端 " + (c.version || ""));
    led("ledCli", "ledCliT", c.cli_ok ? "ok" : "bad", c.cli_ok ? "零调用可用" : "零调用不可用");
    led("ledKey", "ledKeyT", c.key_ready ? "ok" : "warn", c.key_ready ? "Key 就绪" : "无 Key（④不可用）");
    $("cardRun").className = "card" + (c.key_ready ? "" : " off");
    $("cardConn").className = "card" + (c.token_required ? "" : " off");
    $("runBtn").disabled = !c.key_ready;
    if (c.token_required && !token()) $("tokenIn").placeholder = "本服务设了 PM_API_TOKEN，必填";
    if (c.note) $("checkMeta").textContent = c.note;
  }catch(e){
    led("ledBack", "ledBackT", "bad", "后端离线");
    led("ledCli", "ledCliT", "", "零调用");
    led("ledKey", "ledKeyT", "", "Key");
    showErr("checkErr", e);
  }
}

// ---- ① 体检 ----
function findingRows(items, cls){
  if (!items || !items.length) return "";
  let h = '<ul class="f">';
  for (const f of items){
    h += '<li' + (cls ? ' class="' + cls + '"' : "") + '><span class="fcode">' + esc(f.code)
      + "</span><span class=\"fdetail\">" + esc(f.detail) + "</span></li>";
  }
  return h + "</ul>";
}
function renderCheck(d){
  const okv = d.ok === true;
  let h = '<p class="verdict ' + (okv ? "ok" : "bad") + '">'
    + (okv ? "无命中：确定性规则一条都没拦到" : "命中 " + (d.findings || []).length + " 条（退出码 1）")
    + "</p>";
  h += '<div class="kv">'
    + "<span>字符 <b>" + n(d.chars) + "</b></span>"
    + "<span>行 <b>" + n(d.lines) + "</b></span>"
    + "<span>约束 <b>" + n(d.constraints) + " / " + n(d.constraint_limit) + "</b></span>"
    + "<span>结构条目 <b>" + n(d.structure_items) + "</b></span>"
    + "<span>定界符问题 <b>" + (d.delimiter_problems || []).length + "</b></span>"
    + "</div>";
  h += findingRows(d.findings, "");
  if (!d.findings || !d.findings.length) h += '<p class="muted">这条只证「没踩已知事故模式」，不证任务能完成。</p>';
  $("checkOut").innerHTML = h;
}
async function runCheck(){
  const p = $("promptIn").value;
  clearErr("checkErr"); $("checkOut").innerHTML = "";
  if (!p.trim()){ showErr("checkErr", new Error("先把要体检的提示词贴进来")); return; }
  $("checkBtn").disabled = true;
  try{ renderCheck(await api("/api/panel/check", "POST", {prompt: p})); }
  catch(e){ showErr("checkErr", e); }
  finally{ $("checkBtn").disabled = false; }
}

// ---- ② 考卷 ----
async function loadTemplates(){
  try{
    const t = await api("/api/panel/templates");
    let h = '<option value="">（粘贴自己的 JSON 数组）</option>';
    for (const one of t.templates){
      h += '<option value="' + esc(one.name) + '">' + esc(one.name) + "（" + n(one.cases) + " 条）</option>";
    }
    $("tplSel").innerHTML = h;
  }catch(e){ $("tplSel").innerHTML = '<option value="">模板载入失败</option>'; showErr("casesErr", e); }
}
function bucketBars(d){
  const buckets = [
    ["strong", "强判据（字面/正则能否决）", n(d.veto_strong)],
    ["echo", "只挡得住丢内容", n(d.echo_only)],
    ["judge", "交评委（rule）", n(d.judge_only)],
    ["dead", "空 expected 不执行", n(d.no_assertion)]
  ];
  const total = buckets.reduce((a, b) => a + b[2], 0) || 1;
  let bar = '<div class="bar">', leg = '<div class="legend">';
  for (const b of buckets){
    if (b[2] > 0) bar += '<i class="' + b[0] + '" data-w="' + Math.round(100 * b[2] / total) + '"></i>';
    leg += '<span><i class="dot ' + b[0] + '"></i>' + esc(b[1]) + " <b>" + b[2] + "</b></span>";
  }
  // 宽度用 CSSOM 设，不用 style 属性（nonce CSP 下属性内联会被拦）
  bar += "</div>";
  leg += "</div>";
  return bar + leg;
}
function renderCases(d){
  const total = n(d.cases), strong = n(d.veto_strong), able = n(d.veto_capable);
  let h = '<p class="verdict ' + (strong > 0 ? "ok" : "bad") + '">'
    + total + " 条里 <b>" + able + "</b> 条能否决，其中强判据 <b>" + strong + "</b> 条"
    + (strong === 0 ? " —— 这份考卷拦不住「写得漂亮但做不到」" : "") + "</p>";
  h += bucketBars(d);
  h += '<div class="kv"><span>上限 <b>' + n(d.max_cases) + "</b></span>"
    + "<span>提醒项 <b>" + n(d.advisory) + "</b></span>"
    + "<span>强判据在第 <b>" + esc((d.veto_strong_cases || []).join(", ") || "—") + "</b> 条</span></div>";
  h += findingRows(d.findings, "judge");
  $("casesOut").innerHTML = h;
  const bars = $("casesOut").querySelectorAll(".bar i");
  for (const el of bars){ el.style.width = (el.getAttribute("data-w") || "0") + "%"; }
}
async function runCases(){
  clearErr("casesErr"); $("casesOut").innerHTML = "";
  const name = $("tplSel").value;
  if (name){
    $("casesBtn").disabled = true;
    try{ renderCases(await api("/api/panel/cases-check", "POST", {template: name})); }
    catch(e){ showErr("casesErr", e); }
    finally{ $("casesBtn").disabled = false; }
    return;
  }
  const raw = $("casesIn").value.trim();
  if (!raw){ showErr("casesErr", new Error("选一个模板，或粘一份 JSON 数组")); return; }
  let parsed;
  try{ parsed = JSON.parse(raw); }
  catch(e){ showErr("casesErr", new Error("JSON 解析失败：" + e.message)); return; }
  $("casesIn").value = JSON.stringify(parsed, null, 2);
  $("casesBtn").disabled = true;
  try{ renderCases(await api("/api/panel/cases-check", "POST", {cases: $("casesIn").value})); }
  catch(e){ showErr("casesErr", e); }
  finally{ $("casesBtn").disabled = false; }
}

// ---- ③ 差分 ----
function renderDiff(d){
  const r = d.rules || {};
  const fixed = (r.fixed || []).length, intro = (r.introduced || []).length, still = (r.still || []).length;
  let h = '<p class="verdict ' + (d.regressed ? "bad" : "ok") + '">' + esc(d.summary || "") + "</p>";
  h += '<div class="kv"><span>修好 <b>' + fixed + "</b></span>"
    + "<span>新引入 <b>" + intro + "</b></span>"
    + "<span>仍在 <b>" + still + "</b></span>"
    + "<span>约束条目 <b>" + n((d.structure || {}).items ? d.structure.items.before : 0)
    + " → " + n((d.structure || {}).items ? d.structure.items.after : 0) + "</b></span>"
    + "<span>逐字相同 <b>" + (d.identical ? "是" : "否") + "</b></span></div>";
  if (intro) h += '<p class="muted">新引入的这些就是 CI 该拦的那一半：</p>'
    + findingRows(r.introduced.map((c) => ({code: c, detail: "本次新引入的已知事故模式"})), "judge");
  if (d.unified){
    // 头两行是 CLI 临时目录里的真实路径（C:\\Users\\…\\a.md）——面板不该把宿主路径给用户看
    const lines = d.unified.split("\n");
    if (lines.length > 1){ lines[0] = "--- 改动前"; lines[1] = "+++ 改动后"; }
    h += '<pre class="uni">' + esc(lines.join("\n")) + "</pre>";
  }
  $("diffOut").innerHTML = h;
}
async function runDiff(){
  clearErr("diffErr"); $("diffOut").innerHTML = "";
  const a = $("diffA").value, b = $("diffB").value;
  if (!a.trim() || !b.trim()){ showErr("diffErr", new Error("两版都要填")); return; }
  $("diffBtn").disabled = true;
  try{ renderDiff(await api("/api/panel/diff", "POST", {before: a, after: b})); }
  catch(e){ showErr("diffErr", e); }
  finally{ $("diffBtn").disabled = false; }
}

// ---- ④ 优化闭环 ----
let pollTimer = null, curRun = null;
function kvAgg(g, label){
  if (!g) return "<span>" + esc(label) + " <b>—</b></span>";
  return "<span>" + esc(label) + " <b>" + (g.avg_score == null ? "—" : n(g.avg_score).toFixed ? Number(g.avg_score).toFixed(2) : esc(g.avg_score)) + "</b></span>";
}
function renderRun(s){
  const agg = s.aggregate || {}, base = s.baseline_aggregate || {};
  const pw = s.pairwise || {};
  let h = '<div class="kv"><span>状态 <b>' + esc(s.status) + "</b></span>"
    + "<span>轮次 <b>" + n(s.iteration) + "</b></span>"
    + "<span>调用 <b>" + n(s.llm_calls) + "</b></span>"
    + "<span>用例 <b>" + n(agg.n_cases) + "</b></span>"
    + kvAgg(base, "基线") + kvAgg(agg, "优化后")
    + "<span>盲评 <b>" + esc(pw.verdict || "—") + "</b></span></div>";
  if (s.error) h += '<p class="err">任务报错：' + esc(s.error) + "</p>";
  if (s.early_stop_reason) h += '<p class="muted">早停：' + esc(s.early_stop_reason) + "</p>";
  $("runOut").innerHTML = h;
  $("runMeta").textContent = (curRun || "") + " · 每 5 秒轮询";
}
async function pollRun(){
  if (!curRun) return;
  try{
    const s = await api("/api/status/" + encodeURIComponent(curRun));
    renderRun(s);
    if (["passed", "max_iterations", "failed", "early_stopped", "needs_clarification"].indexOf(s.status) >= 0){
      clearInterval(pollTimer); pollTimer = null;
      $("runBtn").disabled = false; $("reportBtn").disabled = false;
      $("runMeta").textContent = curRun + " · 已终态";
    }
  }catch(e){
    clearInterval(pollTimer); pollTimer = null;
    $("runBtn").disabled = false;
    showErr("runErr", e);
  }
}
async function submitRun(){
  clearErr("runErr"); $("runOut").innerHTML = ""; $("runReport").innerHTML = "";
  const task = $("taskIn").value.trim();
  if (task.length < 4){ showErr("runErr", new Error("需求至少 4 个字")); return; }
  $("runBtn").disabled = true; $("reportBtn").disabled = true;
  try{
    const d = await api("/api/optimize", "POST", {
      task: task,
      target_model: $("modelIn").value.trim() || "未指定",
      n_test_cases: 4,
      max_iterations: 2
    });
    curRun = d.run_id;
    $("runMeta").textContent = curRun + " · 提交成功，开始轮询";
    pollRun();
    clearInterval(pollTimer);
    pollTimer = setInterval(pollRun, 5000);
  }catch(e){ showErr("runErr", e); $("runBtn").disabled = false; }
}
async function loadReport(){
  clearErr("runErr");
  if (!curRun){ showErr("runErr", new Error("还没有 run_id")); return; }
  try{
    const d = await api("/api/report/" + encodeURIComponent(curRun));
    // 报告写进自己那块，不写 #runOut —— 否则下一个 5 秒轮询会把整份报告刷掉
    // （2026-10-14 浏览器里点出来的：报告确实取到了，却在下一页轮询到达时消失）
    $("runReport").innerHTML = '<p class="verdict na">交付报告全文（原样转义显示，不渲染 HTML）</p>'
      + '<pre class="uni">' + esc(d.report) + "</pre>";
  }catch(e){ showErr("runErr", e); }
}

// ---- 绑定 ----
$("checkBtn").addEventListener("click", runCheck);
$("checkClearBtn").addEventListener("click", () => {
  $("promptIn").value = ""; $("checkOut").innerHTML = ""; clearErr("checkErr");
});
$("casesBtn").addEventListener("click", runCases);
$("tplSel").addEventListener("change", () => {
  clearErr("casesErr"); $("casesOut").innerHTML = "";
  if ($("tplSel").value) $("casesIn").value = "";
});
$("diffBtn").addEventListener("click", runDiff);
$("diffSwapBtn").addEventListener("click", () => { $("diffA").value = $("promptIn").value; });
$("runBtn").addEventListener("click", submitRun);
$("reportBtn").addEventListener("click", loadReport);
$("tokenBtn").addEventListener("click", () => {
  const t = $("tokenIn").value.trim();
  if (t) sessionStorage.setItem("pm.panel.token", t); else sessionStorage.removeItem("pm.panel.token");
  loadCaps();
});
$("tokenIn").value = token();
loadCaps();
loadTemplates();"""

PANEL_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN">\n'
    "<head>\n"
    '<meta charset="UTF-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
    "<title>PromptMaster 面板</title>\n"
    '<style nonce="__CSP_NONCE__">\n'
    f"{PANEL_CSS}\n"
    "</style>\n"
    "</head>\n"
    f"{PANEL_BODY}"
    "\n"
    '<script nonce="__CSP_NONCE__">\n'
    f"{PANEL_JS}\n"
    "</script>\n"
    "</body>\n"
    "</html>\n"
)

__all__ = ["PANEL_BODY", "PANEL_CSS", "PANEL_HTML", "PANEL_JS"]
