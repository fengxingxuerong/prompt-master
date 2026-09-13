/**
 * 控制台浏览器运行时校验（真实 Chromium + 真实 HTTP）。
 *
 * 为什么需要它：`tests/test_web_console.py` 能验证响应头与页面源码，
 * 但"策略到底有没有拦下来"只有真实浏览器才能证明。本脚本补的就是运行时证据：
 *  1. 加载期无 CSP 违规（securitypolicyviolation 事件计数）
 *  2. 事件委托可用：点 tab 真的切面板（data-* 委托绑定生效）
 *  3. 正向对照：内联 style 属性 **必须被 CSP 拦掉**（证明策略在生效，而非只是响应头好看）
 *  4. XSS 回归：md2html 对围栏内的 <img onerror> 保持转义，插进 DOM 也不产生元素
 *  5. 无未捕获 JS 错误（排除第 3 步故意触发的那条）
 *  6. 无真实资源加载失败（favicon 除外）
 *
 * 依赖（二选一）：
 *   A. node 路线（推荐，无需下载浏览器）：`npm i -D playwright-core` + 本地已有 chromium
 *   B. python 路线：`pip install playwright && playwright install chromium`
 *
 * 用法：
 *   node examples/browser_runtime_check.cjs http://127.0.0.1:8080/
 * 可选环境变量：
 *   CHROME_PATH  chromium 可执行文件路径
 *   PW_CORE      playwright-core 的 require 根路径（默认取工作区 node 目录）
 *
 * 退出码：0 = 全通过；1 = 有断言失败；2 = 运行异常（浏览器起不来等）
 */
const path = require('path');

const PW_CORE_ROOT =
  process.env.PW_CORE || 'C:/Users/Admin（无密码）/.workbuddy/binaries/node/workspace/';
const CHROME =
  process.env.CHROME_PATH ||
  'C:/Users/Admin（无密码）/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const URL = process.argv[2] || 'http://127.0.0.1:8080/';

let chromium;
try {
  // playwright-core 通常不在被测项目里，这里按绝对路径解析（避免污染项目依赖）
  chromium = require(path.join(PW_CORE_ROOT, 'node_modules/playwright-core')).chromium;
} catch (e) {
  chromium = require('playwright-core').chromium;
}

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext();
  // CSP 违规监听必须在页面脚本之前注入
  await context.addInitScript(() => {
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => {
      window.__csp.push(`${e.violatedDirective} :: ${e.blockedURI}`);
    });
  });
  const page = await context.newPage();
  const consoleErrors = [];
  const failedRequests = [];
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text());
  });
  page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + e.message));
  page.on('response', (r) => {
    if (r.status() >= 400) failedRequests.push(`${r.status()} ${r.url()}`);
  });
  page.on('requestfailed', (r) => failedRequests.push(`FAILED ${r.url()}`));

  await page.goto(URL, { waitUntil: 'load' });
  await page.waitForTimeout(300);

  let csp = await page.evaluate(() => window.__csp);
  record('加载期无 CSP 违规', csp.length === 0, csp.length ? JSON.stringify(csp) : 'violations=0');

  const tabCount = await page.locator('#tabs .tab').count();
  await page.click('#tabs .tab[data-tab="pipeline"]');
  await page.waitForTimeout(150);
  const pipelineActive = await page
    .locator('#pane-pipeline')
    .evaluate((el) => el.classList.contains('active'));
  record(
    'tab 事件委托生效（切换到流水线面板）',
    tabCount === 4 && pipelineActive,
    `tabs=${tabCount}, active=${pipelineActive}`
  );

  const styleBlocked = await page.evaluate(() => {
    const d = document.createElement('div');
    d.setAttribute('style', 'color: rgb(1, 2, 3)');
    document.body.appendChild(d);
    const applied = getComputedStyle(d).color;
    d.remove();
    return applied !== 'rgb(1, 2, 3)';
  });
  csp = await page.evaluate(() => window.__csp);
  record(
    '内联 style 属性被 CSP 拦掉（正向对照）',
    styleBlocked && csp.some((v) => v.includes('style-src')),
    `computed 未生效=${styleBlocked}, violations=${csp.length}`
  );

  const xss = await page.evaluate(() => {
    const payload = '```\n<img src=x onerror="window.__xss=1">\n```';
    const html = typeof md2html === 'function' ? md2html(payload) : null;
    if (html === null) return { available: false };
    const host = document.createElement('div');
    host.innerHTML = html;
    document.body.appendChild(host);
    const imgs = host.querySelectorAll('img').length;
    const escaped = html.includes('&lt;img') && !html.includes('<img');
    host.remove();
    return { available: true, escaped, imgs, xssFired: window.__xss === 1 };
  });
  record(
    'XSS 回归：围栏内 <img onerror> 保持转义',
    xss.available && xss.escaped && xss.imgs === 0 && !xss.xssFired,
    JSON.stringify(xss)
  );

  const unexpected = consoleErrors.filter(
    (t) => !t.includes('Content Security Policy') && !t.includes('status of 404')
  );
  record(
    '无未捕获 JS 错误（排除故意触发项）',
    unexpected.length === 0,
    unexpected.slice(0, 3).join(' | ') || 'errors=0'
  );

  const realFailures = failedRequests.filter((u) => !u.includes('favicon'));
  record(
    '无真实资源加载失败（favicon 除外）',
    realFailures.length === 0,
    realFailures.slice(0, 3).join(' | ') || `failures=${failedRequests.length}`
  );

  await browser.close();
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n汇总：${results.length - failed}/${results.length} 通过`);
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error('运行失败：', e && e.message);
  process.exit(2);
});
