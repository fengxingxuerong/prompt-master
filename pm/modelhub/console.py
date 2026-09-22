"""轻量 Web 控制台（OPT-4）：池状态 / 台账 / 虚拟密钥 / 对话测试。

单页自包含 HTML（Fathom 严谨图表风：直角 + hairline + 单强调色），
数据经 fetch 调网关自身 API（/v1/pool/status、/v1/ledger、/v1/keys、/v1/chat/completions）。
"""

CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ModelHub 控制台</title>
<meta name="description" content="模型池网关控制台：池状态、调用台账、虚拟密钥与对话测试。">
<style>
  :root { --paper:#faf9f7; --ink:#1e2a38; --soft:#4a5a6a; --faint:#8494a4;
    --line:#d8d4cc; --line2:#e8e4dc; --accent:#0f4c81; --wash:#eef3f7;
    --ok:#2e6e4e; --bad:#8a3a3a; --mono:Consolas,monospace; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--paper); color:var(--ink); font-family:"Microsoft YaHei",sans-serif; font-size:14px; line-height:1.55; }
  .wrap { max-width:1080px; margin:0 auto; padding:28px 24px 80px; }
  header { border-bottom:2px solid var(--ink); padding-bottom:14px; margin-bottom:20px; display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }
  h1 { font-size:22px; font-weight:650; letter-spacing:-0.01em; }
  .k { font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--faint); }
  nav { display:flex; gap:0; border-bottom:1px solid var(--line); margin-bottom:20px; }
  nav button { background:none; border:none; border-bottom:2px solid transparent; padding:9px 16px; font-size:13.5px; color:var(--soft); cursor:pointer; font-family:inherit; }
  nav button.on { color:var(--accent); border-bottom-color:var(--accent); font-weight:600; }
  section { display:none; } section.on { display:block; }
  table { width:100%; border-collapse:collapse; font-size:13px; margin-top:10px; }
  th { text-align:left; font-size:11px; letter-spacing:.07em; text-transform:uppercase; color:var(--faint); padding:7px 9px; border-top:2px solid var(--ink); border-bottom:1px solid var(--line); }
  td { padding:7px 9px; border-bottom:1px solid var(--line2); vertical-align:top; }
  .mono { font-family:var(--mono); font-size:12px; }
  .ok { color:var(--ok); font-weight:600; } .bad { color:var(--bad); font-weight:600; } .off { color:var(--faint); }
  .toolbar { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0; }
  input, select, textarea { border:1px solid var(--line); background:#fff; padding:7px 9px; font-size:13px; font-family:inherit; }
  input:focus, textarea:focus { outline:2px solid var(--accent); outline-offset:-1px; }
  button.act { background:var(--accent); color:#fff; border:none; padding:8px 16px; cursor:pointer; font-size:13px; font-family:inherit; }
  button.act:disabled { background:var(--faint); cursor:not-allowed; }
  button.ghost { background:none; color:var(--accent); border:1px solid var(--accent); padding:5px 10px; cursor:pointer; font-size:12px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:0; border-top:2px solid var(--ink); border-bottom:1px solid var(--line); }
  .cell { padding:14px 16px; border-right:1px solid var(--line2); }
  .cell .n { font-size:26px; font-weight:700; font-family:Georgia,serif; }
  .cell .l { font-size:12px; color:var(--soft); }
  .msg { padding:9px 12px; border-left:3px solid var(--accent); background:var(--wash); font-size:13px; margin:10px 0; display:none; }
  .msg.show { display:block; }
  .msg.err { border-left-color:var(--bad); }
  pre.out { background:#fff; border:1px solid var(--line); padding:10px; font-size:12px; font-family:var(--mono); white-space:pre-wrap; max-height:260px; overflow:auto; }
  label { font-size:12px; color:var(--soft); display:block; margin:8px 0 3px; }
</style>
</head>
<body>
<div class="wrap">
<header>
  <div><div class="k">ModelHub Gateway</div><h1>控制台</h1></div>
  <div class="k mono" id="ver">v?</div>
</header>
<nav>
  <button data-s="pool" class="on">模型池</button>
  <button data-s="chat">对话测试</button>
  <button data-s="ledger">调用台账</button>
  <button data-s="keys">虚拟密钥</button>
</nav>

<section id="s-pool" class="on">
  <div class="grid" id="poolMetrics"></div>
  <h3 style="margin:18px 0 4px;font-size:15px;">池内模型（主备链）</h3>
  <table><thead><tr><th>模型</th><th>优先级</th><th>断路器</th><th>连败</th><th>最近错误</th></tr></thead><tbody id="poolBody"></tbody></table>
  <div style="margin-top:16px;"><div class="k">指标 /v1/metrics</div><pre class="out" id="metricsOut">…</pre></div>
</section>

<section id="s-chat">
  <label>角色（role，可空）</label>
  <input id="cRole" placeholder="assistant / analyst / …" style="width:260px">
  <label>模型（可空 = 主备链自动）</label>
  <input id="cModel" placeholder="留空自动" style="width:260px">
  <label>消息</label>
  <textarea id="cMsg" rows="3" style="width:100%">你好，请用一句话介绍你自己。</textarea>
  <div class="toolbar">
    <button class="act" id="cSend">发送（非流式）</button>
    <button class="act" id="cStream" style="background:#2e6e4e;">发送（流式）</button>
    <input id="cAdmin" placeholder="管理员口令（未设网关令牌可空）" style="flex:1;min-width:200px">
  </div>
  <div class="msg" id="cNote"></div>
  <pre class="out" id="cOut">…</pre>
</section>

<section id="s-ledger">
  <div class="toolbar">
    <select id="lType"><option value="">全部类型</option><option value="call">call</option><option value="switch">switch</option></select>
    <select id="lSucc"><option value="">全部结果</option><option value="1">成功</option><option value="0">失败</option></select>
    <button class="act" id="lLoad">刷新</button>
  </div>
  <table><thead><tr><th>时间</th><th>类型</th><th>模型</th><th>结果</th><th>耗时</th><th>切换</th><th>agent</th><th>错误</th></tr></thead><tbody id="ledgerBody"></tbody></table>
</section>

<section id="s-keys">
  <div class="toolbar">
    <input id="kAgent" placeholder="绑定 agent 名称" style="width:220px">
    <button class="act" id="kIssue">签发新密钥</button>
  </div>
  <div class="msg" id="kNote"></div>
  <table><thead><tr><th>密钥</th><th>agent</th><th>状态</th><th>签发时间</th><th>操作</th></tr></thead><tbody id="keysBody"></tbody></table>
</section>
</div>
<script>
let ADMIN = "";
const $ = s => document.querySelector(s);
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.remove("on"));
  document.querySelectorAll("section").forEach(x => x.classList.remove("on"));
  b.classList.add("on"); $("#s-" + b.dataset.s).classList.add("on");
  if (b.dataset.s === "pool") loadPool();
  if (b.dataset.s === "ledger") loadLedger();
  if (b.dataset.s === "keys") loadKeys();
});
function hdr() { return ADMIN ? { "X-API-Key": ADMIN, "Content-Type": "application/json" } : { "Content-Type": "application/json" }; }
function note(sel, text, isErr) { const e = $(sel); e.textContent = text; e.className = "msg show" + (isErr ? " err" : ""); }
async function loadPool() {
  try {
    const s = await (await fetch("/v1/pool/status", { headers: hdr() })).json();
    const models = s.models || [];
    const openCnt = models.filter(m => m.circuit !== "closed").length;
    $("#poolMetrics").innerHTML =
      `<div class="cell"><div class="n">${models.length}</div><div class="l">池内模型</div></div>` +
      `<div class="cell"><div class="n ${openCnt ? "bad" : "ok"}">${models.filter(m => m.enabled).length}</div><div class="l">启用</div></div>` +
      `<div class="cell"><div class="n ${openCnt ? "bad" : "ok"}">${openCnt}</div><div class="l">熔断冷却中</div></div>` +
      `<div class="cell"><div class="n">${s.break_threshold}</div><div class="l">熔断阈值（连败）</div></div>`;
    $("#poolBody").innerHTML = models.map(m =>
      `<tr><td class="mono">${m.name}</td><td>${m.priority}</td>` +
      `<td class="${m.circuit === "closed" ? "ok" : "bad"}">${m.circuit}</td><td>${m.consecutive_failures}</td>` +
      `<td class="mono" style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${m.last_error || "—"}</td></tr>`).join("");
    const mt = await (await fetch("/v1/metrics", { headers: hdr() })).json();
    $("#metricsOut").textContent = JSON.stringify(mt, null, 1);
    $("#ver").textContent = "v1.1.0";
  } catch (e) { note("#cNote", "加载池状态失败：" + e, true); }
}
async function loadLedger() {
  const t = $("#lType").value, s = $("#lSucc").value;
  const q = new URLSearchParams(); if (t) q.set("type", t); if (s) q.set("success", s); q.set("limit", "60");
  try {
    const d = await (await fetch("/v1/ledger?" + q, { headers: hdr() })).json();
    $("#ledgerBody").innerHTML = (d.rows || []).map(r =>
      `<tr><td class="mono">${(r.ts || "").replace("T", " ").slice(0, 19)}</td>` +
      `<td>${r.type}</td><td class="mono">${r.model || r.to_model || "—"}</td>` +
      `<td>${r.type === "switch" ? '<span class="off">切换</span>' : (r.success ? '<span class="ok">成功</span>' : '<span class="bad">失败</span>')}</td>` +
      `<td>${r.latency_ms != null ? r.latency_ms + "ms" : "—"}</td>` +
      `<td>${r.failovers != null ? r.failovers : (r.failover_index != null ? "#" + r.failover_index : "—")}</td>` +
      `<td class="mono">${r.agent || "—"}</td>` +
      `<td class="mono" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${r.error_msg || r.reason || "—"}</td></tr>`).join("");
  } catch (e) { alert("台账加载失败：" + e); }
}
async function loadKeys() {
  try {
    const d = await (await fetch("/v1/keys", { headers: hdr() })).json();
    $("#keysBody").innerHTML = (d.keys || []).map(k =>
      `<tr><td class="mono">${k.key.slice(0, 10)}…${k.key.slice(-4)}</td><td class="mono">${k.agent}</td>` +
      `<td class="${k.enabled ? "ok" : "off"}">${k.enabled ? "启用" : "停用"}</td><td class="mono">${(k.issued_at || "").slice(0, 19)}</td>` +
      `<td><button class="ghost" onclick="toggleKey('${k.key}',${!k.enabled})">${k.enabled ? "停用" : "启用"}</button> ` +
      `<button class="ghost" onclick="delKey('${k.key}')">删除</button></td></tr>`).join("") || '<tr><td colspan="5" class="off">暂无密钥，签发后智能体可用 vk-… 调用对话接口</td></tr>';
  } catch (e) { note("#kNote", "密钥加载失败（需管理员口令？）：" + e, true); }
}
async function toggleKey(key, enable) {
  const r = await fetch("/v1/keys/" + key, { method: "PATCH", headers: hdr(), body: JSON.stringify({ enabled: enable }) });
  note("#kNote", r.ok ? "已更新" : "更新失败 HTTP " + r.status, !r.ok); loadKeys();
}
async function delKey(key) {
  const r = await fetch("/v1/keys/" + key, { method: "DELETE", headers: hdr() });
  note("#kNote", r.ok ? "已删除" : "删除失败 HTTP " + r.status, !r.ok); loadKeys();
}
$("#kIssue").onclick = async () => {
  const a = $("#kAgent").value.trim(); if (!a) return note("#kNote", "请填 agent 名称", true);
  const r = await fetch("/v1/keys", { method: "POST", headers: hdr(), body: JSON.stringify({ agent: a }) });
  if (r.ok) { const d = await r.json(); note("#kNote", "已签发：" + d.key.key + "（请立即复制，之后只显示尾 4 位）"); loadKeys(); }
  else note("#kNote", "签发失败 HTTP " + r.status + "（需管理员口令？）", true);
};
$("#cSend").onclick = doChat(false);
$("#cStream").onclick = doChat(true);
$("#cAdmin").addEventListener("change", e => { ADMIN = e.target.value.trim(); });
function doChat(stream) {
  return async () => {
    const btn = stream ? $("#cStream") : $("#cSend"); btn.disabled = true;
    const body = { messages: [{ role: "user", content: $("#cMsg").value }], stream: stream };
    if ($("#cRole").value.trim()) body.role = $("#cRole").value.trim();
    if ($("#cModel").value.trim()) body.model = $("#cModel").value.trim();
    const t0 = performance.now();
    try {
      if (!stream) {
        const r = await fetch("/v1/chat/completions", { method: "POST", headers: hdr(), body: JSON.stringify(body) });
        const d = await r.json();
        if (r.ok) {
          const mh = d.modelhub || {};
          $("#cOut").textContent = d.choices[0].message.content +
            `\n\n--- model=${d.model} failovers=${mh.failovers} latency=${mh.latency_ms}ms rid=${mh.request_id}` +
            `\n总耗时 ${Math.round(performance.now() - t0)}ms`;
          note("#cNote", "成功");
        } else { $("#cOut").textContent = JSON.stringify(d, null, 1); note("#cNote", "失败 HTTP " + r.status, true); }
      } else {
        const r = await fetch("/v1/chat/completions", { method: "POST", headers: hdr(), body: JSON.stringify(body) });
        if (!r.ok) { const d = await r.json().catch(() => ({})); $("#cOut").textContent = JSON.stringify(d, null, 1); note("#cNote", "失败 HTTP " + r.status, true); btn.disabled = false; return; }
        const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "", parts = [], model = "?", done = false;
        $("#cOut").textContent = "";
        while (true) {
          const { done: rd, value } = await reader.read(); if (rd) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split("\n\n"); buf = lines.pop();
          for (const ln of lines) {
            const line = ln.trim(); if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trim();
            if (payload === "[DONE]") { done = true; continue; }
            try { const o = JSON.parse(payload); if (o.error) { note("#cNote", "流内错误：" + o.error.message, true); }
              model = o.model || model;
              for (const ch of o.choices || []) { const p = (ch.delta || {}).content || ""; if (p) parts.push(p); } } catch (e) {}
          }
          $("#cOut").textContent = parts.join("") + (done ? "" : " ▌");
        }
        $("#cOut").textContent = parts.join("") + `\n\n--- [流式] model=${model} 总耗时 ${Math.round(performance.now() - t0)}ms`;
        note("#cNote", "流式完成");
      }
    } catch (e) { note("#cNote", "请求异常：" + e, true); }
    btn.disabled = false;
  };
}
loadPool();
</script>
</body>
</html>
"""
