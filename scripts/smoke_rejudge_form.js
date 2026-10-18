/* 用法：node scripts/smoke_rejudge_form.js [html路径]
 * 依赖 jsdom：优先用 WS 指向的托管 workspace，其次按普通模块解析。
 * 判据只覆盖"人能不能真判"这条线：完整输出、转义、进度记账、导出列与行数。
 */
const fs = require("fs");
const path = require("path");

const ws = process.env.WS || "";
let JSDOM;
try {
  JSDOM = require(ws ? path.join(ws, "node_modules", "jsdom") : "jsdom").JSDOM;
} catch (e) {
  console.error("jsdom 不可用：" + e.message);
  process.exit(3);
}

const file = process.argv[2]
  || path.join(__dirname, "..", "judge_calibration", "rejudge_form.html");
const html = fs.readFileSync(file, "utf-8");
// 页面可以被生成到任何地方（测试就是生成到 tmp_path 的），所以锚点 JSON 由调用方指定
const srcJson = process.argv[3] || path.join(path.dirname(file), "samples.candidates.json");

const fail = [];
const ok = (cond, msg) => { if (!cond) fail.push(msg); else console.log("  ok  " + msg); };

const dom = new JSDOM(html, { runScripts: "dangerously", url: "https://localhost/rejudge" });
const { window } = dom;
const doc = window.document;

const cards = doc.querySelectorAll(".card");
ok(cards.length === 25, `渲染出 25 张卡（实际 ${cards.length}）`);

const pres = [...doc.querySelectorAll(".card pre")];
ok(pres.length >= 25, `每条都展示了输出（pre 数 ${pres.length}）`);
ok(pres.every(p => (p.textContent || "").trim().length > 0), "没有空输出卡");

const longest = Math.max(...pres.map(p => p.textContent.length));
ok(longest > 500, `最长输出完整展开（${longest} 字，CSV 表只给 160 字）`);

// 逐条与源 JSON 比对长度：一个字都不许少（这才是"没截断"的硬判据）
const src = JSON.parse(fs.readFileSync(srcJson, "utf-8"));
const byId = Object.fromEntries(src.filter(x => x && x.id).map(x => [x.id, x.test_output || ""]));
const outPres = [...doc.querySelectorAll(".card")].map(c => {
  const all = [...c.querySelectorAll("pre")];
  return { id: c.id.slice(2), text: (all[all.length - 1].textContent || "") };
});
const short = outPres.filter(x => x.text.length !== (byId[x.id] || "").length);
ok(short.length === 0, `25 条输出与源 JSON 逐字等长（缺 ${short.length} 条）`);

// 转义：输出里含 <输入> 标签的那条必须当文本显示，不能被当成标签吃掉
const tagged = pres.find(p => p.textContent.includes("<输入>"));
if (tagged) {
  ok(!tagged.innerHTML.includes("<输入>"), "含 <输入> 的输出被转义（没有变成真标签）");
} else {
  ok(false, "没找到含 <输入> 的输出，转义检查未执行");
}

// 打 3 条分
const btns = [...doc.querySelectorAll("button[data-quick='8']")].slice(0, 3);
btns.forEach(b => b.dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
const prog = doc.getElementById("prog").textContent;
ok(/已判\s*3/.test(prog.replace(/\s+/g, " ")), `点 3 次「8 可上线」后进度为 3（实际：${prog.trim()}）`);
ok(doc.querySelectorAll(".card.done").length === 3, "已判的卡有完成态标记");

const saved = JSON.parse(window.localStorage.getItem(("pm-rejudge-" + 25)) || "{}");
ok(Object.keys(saved).length === 3, `localStorage 存了 3 条（${Object.keys(saved).length}）`);

// 导出 CSV
let captured = null;
window.URL.createObjectURL = (blob) => { captured = blob; return "blob:fake"; };
doc.getElementById("exp").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
ok(!!captured, "点导出产生了 Blob");

(async () => {
  if (!captured) { report(); return; }
  const text = await captured.text();
  const lines = text.replace(/^\uFEFF/, "").split("\r\n").filter(Boolean);
  ok(lines.length === 4, `导出的 CSV 是 1 表头 + 3 行（实际 ${lines.length}）`);
  const head = lines[0];
  ok(head.includes('"id"') && head.includes('"human_score"') && head.includes('"输出摘要"'),
     "表头与 _FORM_COLUMNS 同列");
  ok(lines[0].startsWith('"'), "表头带引号（含中文字段名的 CSV 不会错位）");
  ok(lines.slice(1).every(l => /,"8(\.0)?",/.test(l) || l.split('","').length >= 8), "数据行列数对齐");
  ok(lines.every(l => l.replace(/"/g, "").replace(/,/g, "").trim().length > 0), "没有空行被导出（留空的不进表）");
  report();
})();

function report() {
  console.log(fail.length ? "\n冒烟失败：" : "\n冒烟全部通过");
  fail.forEach(f => console.log("  FAIL  " + f));
  process.exit(fail.length ? 1 : 0);
}
