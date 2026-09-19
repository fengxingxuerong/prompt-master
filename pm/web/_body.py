"""Web 控制台 HTML 骨架（从原 pm/web.py 拆出，内容与拆分前逐字节一致）。"""

BODY_HTML = """<body>

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
      <div class="tab" data-tab="history">历史</div>
    </div>

    <div class="tab-pane active" id="pane-chat">
      <div class="chat-flow" id="chatFlow"><div class="empty-ws">在底部输入你的提示词需求，agent 会自动澄清、生成、测试、评估、迭代优化</div></div>
    </div>
    <div class="tab-pane" id="pane-pipeline"><div class="pipeline" id="pipeline"><div class="empty-ws">提交任务后，节点级执行轨迹会出现在这里</div></div></div>
    <div class="tab-pane" id="pane-prompt"><div id="promptView"><div class="empty-ws">生成的提示词版本与 diff 会出现在这里</div></div></div>
    <div class="tab-pane" id="pane-score"><div id="scoreView"><div class="empty-ws">评分雷达图、版本曲线、基线 Δ 与盲评结论会出现在这里</div></div></div>
    <div class="tab-pane" id="pane-history"><div id="historyView"><div class="empty-ws">切换到本页查看跨任务历史与 Δ 显著性判定</div></div></div>

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
"""
