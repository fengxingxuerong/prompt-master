"""Web 控制台样式（从原 pm/web.py 拆出，内容与拆分前逐字节一致）。"""

CSS = """  :root{
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
  .u-w100{width:100%}"""
