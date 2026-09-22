# 设计规范（Design Tokens · 三服务统一视觉语言）

> v1.3.0 · 本文件是三服务 UI 的一致性契约。改样式只动各页面 `:root` 变量与本文档同步。

## 色板（按服务定位分工）

| Token | ModelHub 控制台 | 龙虾站 | 任务台账 |
|---|---|---|---|
| 背景 paper | `#0b0f14`（深海） | `#0b0f14` | `#fafaf8`（暖纸白） |
| 面板 panel | `#131b24` | `#131b24` | `#ffffff` |
| 主文字 ink | `#f2f0ea` | `#f2f0ea` | `#1a2530` |
| 次文字 soft | `#b9c0c6` | `#b9c0c6` | `#5a6875` |
| 强调 accent | `#ff6b35`（橙） | `#ff6b35` | `#0f4c81`（海军蓝） |
| 辅助 teal | `#3ec9c0` | `#3ec9c0` | — |
| 成功 ok | `#7ed491` | — | `#2e6e4e` |
| 危险 warn | `#ff6b35` | — | `#c0392b` |
| 分隔线 line | `#243040` | `#243040` | `#dfe3e0` |

规律：深色服务（ModelHub/龙虾）用橙青对撞（Ash Thorp 血统）；浅色管理页（任务台账）用海军蓝强调（Fathom 血统）。

## 字体与字号

- 中文栈：`"Source Han Sans SC", "Noto Sans SC", "Microsoft YaHei", sans-serif`
- 等宽栈：`Consolas, "Cascadia Mono", monospace`（编号、时间戳、金额、JSON）
- 阶梯：页面主标 22–40px / 区块标 15–17px / 正文 13.5–15px / 标注 11–12.5px（大写+0.07em 字距）

## 间距与圆角

- 8pt 网格：所有 padding/margin 是 4 的倍数（4/8/12/16/24/32）
- 区块间距 24–34px；表单字段间 14px；表格行高 36–40px
- 圆角：龙虾站无圆角（锐利电影感）；任务台账无圆角（期刊感）；Takram 风格的圆角柔影仅用于海报装饰性元素

## 交互状态（四态齐全）

| 状态 | 按钮 | 输入框 | 表格行 |
|---|---|---|---|
| hover | 边框/文字变 accent 色 | 边框 accent | 背景加深一档 |
| active/点击 | 背景加深 + translateY(-2px) | — | — |
| focus | — | outline 2px accent（offset -1px） | — |
| disabled | 背景变 faint、cursor:not-allowed | — | — |

错误态：表单字段 `.invalid` 边框变橙 + 下方 12.5px 错误文案；全局消息条左边框 3px accent/橙。

## 响应式断点

- 820px：双栏 → 单栏（龙虾订购区、taskboard 表单）
- 760px：四步冷链 → 2×2；故事区上下排
- 表格横向可滚动（台账类页面）；移动端字号用 clamp() 缩放

## 已落地页面核对基准

| 页面 | 风格 | 自检结论 |
|---|---|---|
| ModelHub /console | 深色控制台 | 四面板、Fetch 自身 API、桌面优先 |
| 龙虾 / /ledger /poster | 深海电影质感 | 响应式 760/820 双断点、内联 SVG、离线可用 |
| 任务台账 / | 浅色期刊看板 | 负载条形图、逾期标红、表格 hover |
